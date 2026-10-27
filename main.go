
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/mark3labs/mcp-go/mcp"
	"github.com/mark3labs/mcp-go/server"
	"github.com/rs/zerolog"
)

var version = "dev"

func main() {
	if err := run(); err != nil {
		if errors.Is(err, context.Canceled) {
			fmt.Fprintln(os.Stderr, "Server stopped gracefully.")
			os.Exit(0)
		}
		fmt.Fprintf(os.Stderr, "Error: %v\n", err)
		os.Exit(1)
	}
	fmt.Fprintln(os.Stderr, "Server exited.")
}

func run() error {
	cfg, err := loadConfig()
	if err != nil {
		return fmt.Errorf("failed to load configuration: %w", err)
	}

	if version != "dev" {
		cfg.Server.Version = version
	}

	log, err := newLogger(cfg.Logging)
	if err != nil {
		return fmt.Errorf("failed to initialize logger: %w", err)
	}

	log.Info().
		Str("server_name", cfg.Server.Name).
		Str("version", cfg.Server.Version).
		Str("stockfish_path", cfg.Stockfish.Path).
		Int("max_sessions", cfg.Stockfish.MaxSessions).
		Str("server_mode", cfg.Server.Mode).
		Msg("Configuration loaded")

	// Preserve Stockfish positions between commands.
	sessionManager := newSessionManager(cfg.Stockfish, log)
	defer sessionManager.Close()

	var executor commandExecutor
	executor = NewPersistentSessionExecutor(sessionManager, log)

	stockfishHandler := newStockfishHandler(executor, log)

	s := server.NewMCPServer(
		cfg.Server.Name,
		cfg.Server.Version,
	)

	stockfishTool := mcp.NewTool(
		"chess_engine",
		mcp.WithDescription(`
Analyze chess positions using the Stockfish UCI engine.

UCI MOVE FORMAT:
e2e4, g1f3, e1g1, e7e8q

POSITION COMMANDS:
position startpos
position startpos moves d2d4 d7d5 c1f4
position fen [FEN]

ANALYSIS COMMANDS:
go depth 20
go movetime 3000
go nodes 100000

ENGINE OPTIONS:
setoption name Hash value 128
setoption name Threads value 1
setoption name MultiPV value 3

SESSION MANAGEMENT:
The first command creates an engine session.
Save the returned session_id.
Reuse that same session_id on subsequent calls
to preserve the position and engine options.

Example:
1. position startpos moves d2d4 d7d5 c1f4
2. Save the returned session_id.
3. go depth 20 using that session_id.

EVALUATION:
100 centipawns = 1 pawn.
Positive evaluations favor White.
Negative evaluations favor Black.
		`),
		mcp.WithString(
			"command",
			mcp.Required(),
			mcp.Description(
				"UCI command, such as position startpos moves d2d4 d7d5 or go depth 20.",
			),
		),
		mcp.WithString(
			"session_id",
			mcp.Description(
				"Optional engine session ID from a previous command. Reuse to preserve the chess position.",
			),
		),
	)

	s.AddTool(stockfishTool, stockfishHandler.handle)

	switch ServerMode(cfg.Server.Mode) {
	case ServerModeHTTP:
		return runHTTPServer(s, cfg, log)
	case ServerModeStdio:
		return runStdioServer(s, log)
	default:
		return fmt.Errorf(
			"unsupported server mode: %s",
			cfg.Server.Mode,
		)
	}
}

func runHTTPServer(
	s *server.MCPServer,
	cfg *Config,
	log zerolog.Logger,
) error {
	addr := fmt.Sprintf(
		"%s:%d",
		cfg.Server.Host,
		cfg.Server.Port,
	)

	audience := os.Getenv("AUTH0_AUDIENCE")
	if audience == "" {
		return errors.New(
			"AUTH0_AUDIENCE must be configured for HTTP mode",
		)
	}

	mcpServer := server.NewStreamableHTTPServer(s)
	mux := http.NewServeMux()

	// Protect all MCP traffic with Auth0.
	mux.Handle(
		"/mcp",
		stockfishAuthMiddleware(mcpServer),
	)

	// Public OAuth protected-resource discovery.
	// This is metadata only, not a chess endpoint.
	metadataHandler := func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")

		metadata := map[string]interface{}{
			"resource": audience,
			"authorization_servers": []string{
				strings.TrimRight(auth0Issuer, "/"),
			},
			"scopes_supported": []string{
				"analyze:chess",
			},
			"bearer_methods_supported": []string{
				"header",
			},
		}

		if err := json.NewEncoder(w).Encode(metadata); err != nil {
			log.Error().Err(err).Msg("OAuth metadata encoding failed")
		}
	}

	mux.HandleFunc(
		"/.well-known/oauth-protected-resource",
		metadataHandler,
	)

	mux.HandleFunc(
		"/.well-known/oauth-protected-resource/mcp",
		metadataHandler,
	)

	// Public health check for Render.
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}

		w.Header().Set(
			"Content-Type",
			"text/plain; charset=utf-8",
		)
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})

	log.Info().
		Str("address", addr).
		Msg("Starting authenticated Stockfish MCP server")

	httpServer := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}

	return httpServer.ListenAndServe()
}

func runStdioServer(
	s *server.MCPServer,
	log zerolog.Logger,
) error {
	log.Info().Msg(
		"MCP Stockfish server initialized, serving on stdio",
	)

	err := server.ServeStdio(s)
	if err != nil {
		if errors.Is(err, context.Canceled) ||
			errors.Is(err, os.ErrClosed) {
			log.Info().
				Err(err).
				Msg("Stdio server connection closed")
			return context.Canceled
		}
		return fmt.Errorf("stdio server error: %w", err)
	}

	log.Info().Msg("Stdio server finished")
	return nil
}
