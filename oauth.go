
package main

import (
	"encoding/json"
	"net/http"
	"os"
	"strings"
)

func oauthMetadataURL() string {
	base := strings.TrimRight(os.Getenv("MCP_PUBLIC_URL"), "/")

	return base + "/.well-known/oauth-protected-resource"
}

func addOAuthDiscovery(mux *http.ServeMux) {
	handler := func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		resource := os.Getenv("AUTH0_AUDIENCE")
		if resource == "" {
			http.Error(w, "Not configured", http.StatusServiceUnavailable)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")

		metadata := map[string]interface{}{
			"resource": resource,
			"authorization_servers": []string{
				"https://dev-izfq8xjvdj2uhm4t.us.auth0.com/",
			},
			"scopes_supported": []string{
				"analyze:chess",
			},
			"bearer_methods_supported": []string{
				"header",
			},
		}

		_ = json.NewEncoder(w).Encode(metadata)
	}

	mux.HandleFunc(
		"/.well-known/oauth-protected-resource",
		handler,
	)

	mux.HandleFunc(
		"/.well-known/oauth-protected-resource/mcp",
		handler,
	)
}
