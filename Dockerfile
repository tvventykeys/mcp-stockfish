
# Build the MCP server
FROM golang:1.23-bookworm AS builder

WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download

COPY *.go ./
RUN go mod tidy && CGO_ENABLED=0 go build -o mcp-stockfish .

# Runtime with Stockfish
FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      stockfish ca-certificates && \
    ln -s /usr/games/stockfish /usr/bin/stockfish && \
    useradd -m -u 1000 mcpuser && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/mcp-stockfish /usr/local/bin/mcp-stockfish

ENV MCP_STOCKFISH_PATH=/usr/bin/stockfish

USER mcpuser
WORKDIR /home/mcpuser

ENTRYPOINT ["mcp-stockfish"]
