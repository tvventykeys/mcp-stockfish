
package main

import (
	"context"
	"crypto"
	"crypto/rsa"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/big"
	"net/http"
	"os"
	"strings"
	"time"
)

const auth0Issuer = "https://dev-izfq8xjvdj2uhm4t.us.auth0.com/"

type authClaims struct {
	Issuer      string          `json:"iss"`
	Subject     string          `json:"sub"`
	Audience    json.RawMessage `json:"aud"`
	Expires     int64           `json:"exp"`
	NotBefore   int64           `json:"nbf"`
	Scope       string          `json:"scope"`
	Permissions []string        `json:"permissions"`
}

type authKey struct {
	Kid string `json:"kid"`
	Kty string `json:"kty"`
	Use string `json:"use"`
	Alg string `json:"alg"`
	N   string `json:"n"`
	E   string `json:"e"`
}

type authKeySet struct {
	Keys []authKey `json:"keys"`
}

func verifyStockfishToken(ctx context.Context, token string) error {
	audience := os.Getenv("AUTH0_AUDIENCE")
	if audience == "" {
		return errors.New("AUTH0_AUDIENCE is not configured")
	}

	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return errors.New("invalid JWT format")
	}

	headerBytes, err := base64.RawURLEncoding.DecodeString(parts[0])
	if err != nil {
		return errors.New("invalid JWT header")
	}

	var header struct {
		Algorithm string `json:"alg"`
		KeyID     string `json:"kid"`
	}
	if err := json.Unmarshal(headerBytes, &header); err != nil {
		return errors.New("invalid JWT header JSON")
	}
	if header.Algorithm != "RS256" || header.KeyID == "" {
		return errors.New("unsupported token signing algorithm")
	}

	payloadBytes, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return errors.New("invalid JWT payload")
	}

	var claims authClaims
	if err := json.Unmarshal(payloadBytes, &claims); err != nil {
		return errors.New("invalid token claims")
	}

	if claims.Issuer != auth0Issuer {
		return errors.New("invalid token issuer")
	}
	if claims.Subject == "" {
		return errors.New("missing token subject")
	}

	now := time.Now().Unix()
	if claims.Expires <= now {
		return errors.New("token expired")
	}
	if claims.NotBefore != 0 && claims.NotBefore > now {
		return errors.New("token not yet valid")
	}

	var audiences []string
	if err := json.Unmarshal(claims.Audience, &audiences); err != nil {
		var single string
		if err := json.Unmarshal(claims.Audience, &single); err != nil {
			return errors.New("invalid token audience")
		}
		audiences = []string{single}
	}

	matchesAudience := false
	for _, value := range audiences {
		if value == audience {
			matchesAudience = true
		}
	}
	if !matchesAudience {
		return errors.New("token audience mismatch")
	}

	hasPermission := false
	for _, scope := range strings.Fields(claims.Scope) {
		if scope == "analyze:chess" {
			hasPermission = true
		}
	}
	for _, permission := range claims.Permissions {
		if permission == "analyze:chess" {
			hasPermission = true
		}
	}
	if !hasPermission {
		return errors.New("missing analyze:chess permission")
	}

	request, err := http.NewRequestWithContext(
		ctx,
		http.MethodGet,
		auth0Issuer+".well-known/jwks.json",
		nil,
	)
	if err != nil {
		return err
	}

	client := &http.Client{Timeout: 10 * time.Second}
	response, err := client.Do(request)
	if err != nil {
		return fmt.Errorf("unable to retrieve signing keys: %w", err)
	}
	defer response.Body.Close()

	if response.StatusCode != http.StatusOK {
		return errors.New("unable to retrieve Auth0 signing keys")
	}

	var keys authKeySet
	body := io.LimitReader(response.Body, 1024*1024)
	if err := json.NewDecoder(body).Decode(&keys); err != nil {
		return errors.New("invalid Auth0 signing keys")
	}

	var selected *authKey
	for i := range keys.Keys {
		k := &keys.Keys[i]
		if k.Kid == header.KeyID &&
			k.Kty == "RSA" &&
			(k.Use == "" || k.Use == "sig") &&
			(k.Alg == "" || k.Alg == "RS256") {
			selected = k
			break
		}
	}
	if selected == nil {
		return errors.New("unknown signing key")
	}

	modulusBytes, err := base64.RawURLEncoding.DecodeString(selected.N)
	if err != nil {
		return errors.New("invalid signing key modulus")
	}
	exponentBytes, err := base64.RawURLEncoding.DecodeString(selected.E)
	if err != nil {
		return errors.New("invalid signing key exponent")
	}

	exponent := new(big.Int).SetBytes(exponentBytes)
	if !exponent.IsInt64() ||
		exponent.Sign() <= 0 ||
		exponent.Int64() > 2147483647 {
		return errors.New("invalid signing key exponent value")
	}

	publicKey := &rsa.PublicKey{
		N: new(big.Int).SetBytes(modulusBytes),
		E: int(exponent.Int64()),
	}

	signature, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return errors.New("invalid signature encoding")
	}

	signedContent := parts[0] + "." + parts[1]
	digest := sha256.Sum256([]byte(signedContent))

	if err := rsa.VerifyPKCS1v15(
		publicKey,
		crypto.SHA256,
		digest[:],
		signature,
	); err != nil {
		return errors.New("invalid token signature")
	}

	return nil
}

func stockfishAuthMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {

		// Advertise the OAuth discovery endpoint.
		w.Header().Set(
			"WWW-Authenticate",
			`Bearer resource_metadata="`+
				oauthMetadataURL()+
				`", scope="analyze:chess"`,
		)

		authorization := r.Header.Get("Authorization")
		fields := strings.Fields(authorization)

		if len(fields) != 2 ||
			!strings.EqualFold(fields[0], "Bearer") {
			http.Error(
				w,
				"Authentication required",
				http.StatusUnauthorized,
			)
			return
		}

		if err := verifyStockfishToken(r.Context(), fields[1]); err != nil {
			http.Error(
				w,
				"Access denied",
				http.StatusUnauthorized,
			)
			return
		}

		// Remove the authentication challenge on success.
		w.Header().Del("WWW-Authenticate")
		next.ServeHTTP(w, r)
	})
}
