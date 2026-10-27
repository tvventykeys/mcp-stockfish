
# Build the Go MCP server
FROM golang:1.23-alpine AS go-builder

RUN apk add --no-cache git

WORKDIR /app


COPY go.mod go.sum ./
RUN go mod download

COPY *.go ./

# Ensure Go dependencies and checksums are complete
RUN go mod tidy


ARG VERSION=dev

RUN CGO_ENABLED=0 GOOS=linux go build \
    -ldflags "-X main.version=${VERSION} -s -w" \
    -o /app/mcp-stockfish .

# Build official Stockfish 19
FROM debian:bookworm AS stockfish-builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    git \
    curl \
    ca-certificates && \
    rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch sf_19 \
    https://github.com/official-stockfish/Stockfish.git \
    /stockfish

RUN make -C /stockfish/src net

RUN make -C /stockfish/src -j2 build ARCH=x86-64

# Final runtime image
FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates \
    libstdc++6 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=go-builder \
    /app/mcp-stockfish \
    /usr/local/bin/mcp-stockfish

COPY --from=stockfish-builder \
    /stockfish/src/stockfish \
    /usr/bin/stockfish

RUN chmod +x /usr/local/bin/mcp-stockfish \
    /usr/bin/stockfish

# Verify the engine version during build
RUN /usr/bin/stockfish --version || true

# Preserve existing server configuration
ENV MCP_STOCKFISH_PATH=/usr/bin/stockfish

RUN mkdir -p /home/mcpuser && \
    chown -R 10001:10001 /home/mcpuser

USER 10001:10001
WORKDIR /home/mcpuser

EXPOSE 10000

ENTRYPOINT ["/usr/local/bin/mcp-stockfish"]
