
package main

import (
	"context"
	"errors"
	"fmt"
	"os"

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

	// Keep Stockfish processes alive between commands.
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
Advanced chess analysis using Stockfish engine via UCI (Universal Chess Interface).
Analyzes positions, finds best moves, evaluates positions. Returns structured results.

MOVE NOTATION: Use coordinate notation (e2e4, g1f3, e1g1 for castling, e7e8q for promotion)
EVALUATION: Centipawns (100 = 1 pawn), positive = White advantage, negative = Black advantage

ENGINE CONTROL:
uci
isready
quit
stop

POSITION SETUP:
position startpos
position startpos moves e2e4 e7e5
position fen [FEN]
position fen [FEN] moves [MOVES]

ANALYSIS COMMANDS:
go depth [N]
go movetime [MS]
go infinite
go wtime [MS] btime [MS]
go nodes [N]

ENGINE OPTIONS:
setoption name Hash value 128
setoption name Threads value 1
setoption name MultiPV value 3

IMPORTANT SESSION MANAGEMENT:
The first command creates a Stockfish engine session.
The response includes a session_id.
Pass that same session_id in subsequent calls to preserve
the board position and engine settings.

EXAMPLE:
1. command: position startpos moves d2d4 d7d5
2. Save the returned session_id
3. command: go depth 20, with the same session_id
		`),
		mcp.WithString(
			"command",
			mcp.Required(),
			mcp.Description(`
Exact UCI command to execute.

Examples:
position startpos moves d2d4 d7d5
position fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1
go depth 20
setoption name MultiPV value 3
			`),
		),
		mcp.WithString(
			"session_id",
			mcp.Description(
				"Optional Stockfish engine session ID returned by a previous command. Reuse it to preserve the position and engine options.",
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
		return fmt.Errorf("unsupported server mode: %s", cfg.Server.Mode)
	}
}

func runHTTPServer(s *server.MCPServer, cfg *Config, log zerolog.Logger) error {
	addr := fmt.Sprintf("%s:%d", cfg.Server.Host, cfg.Server.Port)

	log.Info().
		Str("address", addr).
		Msg("Starting Stockfish MCP HTTP server")

	httpServer := server.NewStreamableHTTPServer(s)
	return httpServer.Start(addr)
}

func runStdioServer(s *server.MCPServer, log zerolog.Logger) error {
	log.Info().Msg("MCP Stockfish server initialized, serving on stdio")

	err := server.ServeStdio(s)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, os.ErrClosed) {
			log.Info().Err(err).Msg("Stdio server connection closed by client or pipe.")
			return context.Canceled
		}
		return fmt.Errorf("stdio server error: %w", err)
	}

	log.Info().Msg("Stdio server finished.")
	return nil
}
