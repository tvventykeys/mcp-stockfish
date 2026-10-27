
package main

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
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
		Dur("session_timeout", cfg.Stockfish.SessionTimeout).
		Str("server_mode", cfg.Server.Mode).
		Str("http_host", cfg.Server.Host).
		Int("http_port", cfg.Server.Port).
		Msg("Configuration loaded")

	// Preserve Stockfish processes between commands.
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
Advanced chess analysis using the Stockfish engine.

MOVE FORMAT:
Use UCI coordinate notation such as e2e4, g1f3, e1g1.

EVALUATION:
Centipawns (100 = 1 pawn).
Positive scores favor White.
Negative scores favor Black.

ENGINE COMMANDS:
uci
isready
stop
quit

POSITION COMMANDS:
position startpos
position startpos moves e2e4 e7e5
position fen [FEN]

ANALYSIS COMMANDS:
go depth 20
go movetime 3000
go nodes 100000

ENGINE OPTIONS:
setoption name Hash value 128
setoption name Threads value 1
setoption name MultiPV value 3

PERSISTENT SESSIONS:
A command returns an engine session_id.
Reuse that exact session_id for subsequent commands
to preserve the board position and engine settings.

EXAMPLE:
1. command: position startpos moves d2d4 d7d5 c1f4
2. Save the returned session_id.
3. command: go depth 20, using that session_id.
		`),
		mcp.WithString(
			"command",
			mcp.Required(),
			mcp.Description(
				"UCI command, such as position startpos moves d2d4 d7d5, or go depth 20.",
			),
		),
		mcp.WithString(
			"session_id",
			mcp.Description(
				"Optional engine session ID from a previous command. Reuse it to preserve the chess position.",
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

	// Refuse to start without an API audience.
	// This prevents accidentally exposing the server
	// with incomplete authentication configuration.
	if os.Getenv("AUTH0_AUDIENCE") == "" {
		return errors.New(
			"AUTH0_AUDIENCE must be configured for HTTP mode",
		)
	}

	log.Info().
		Str("address", addr).
		Msg("Starting authenticated Stockfish MCP server")

	mcpServer := server.NewStreamableHTTPServer(s)

	mux := http.NewServeMux()

	// Protect all MCP requests with Auth0 verification.
	mux.Handle(
		"/mcp",
		stockfishAuthMiddleware(mcpServer),
	)

	// Public health endpoint for Render.
	// Does not expose chess tools.
	mux.HandleFunc(
		"/health",
		func(w http.ResponseWriter, r *http.Request) {
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
		},
	)

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
	log.Info().
		Msg("MCP Stockfish server initialized, serving on stdio")

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
