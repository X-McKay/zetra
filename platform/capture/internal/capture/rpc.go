package capture

import (
	"bytes"
	"encoding/json"
	"io"
	"sync"
)

const maxRPCBytes = 65536

// rpcBody observes only bytes the proxy actually reads. It never reads ahead, limits
// the forwarding stream, or records a body. Storage is capped independently of the
// request size; overflow/incomplete/ambiguous input produces a fixed sentinel.
type rpcBody struct {
	io.ReadCloser
	mu     sync.Mutex
	prefix []byte
	total  int64
	length int64
	eof    bool
	failed bool
}

func (b *rpcBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	b.mu.Lock()
	defer b.mu.Unlock()
	if n > 0 {
		if b.prefix == nil {
			b.prefix = make([]byte, 0, maxRPCBytes)
		}
		b.total += int64(n)
		remaining := maxRPCBytes - len(b.prefix)
		if n < remaining {
			remaining = n
		}
		b.prefix = append(b.prefix, p[:remaining]...)
	}
	b.eof = b.eof || err == io.EOF
	b.failed = b.failed || (err != nil && err != io.EOF)
	return n, err
}

func (b *rpcBody) method() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	complete := b.length >= 0 && b.total == b.length || b.length < 0 && b.eof
	if b.failed || b.total > maxRPCBytes || !complete {
		return "unclassified"
	}
	// Parse only a single JSON object, rejecting duplicate keys rather than
	// guessing which interpretation an upstream JSON-RPC implementation used.
	d := json.NewDecoder(bytes.NewReader(b.prefix))
	token, err := d.Token()
	if err != nil || token != json.Delim('{') {
		return "unclassified"
	}
	seen := map[string]bool{}
	version, method := "", ""
	for d.More() {
		keyToken, err := d.Token()
		key, ok := keyToken.(string)
		if err != nil || !ok || seen[key] {
			return "unclassified"
		}
		seen[key] = true
		var value json.RawMessage
		if d.Decode(&value) != nil {
			return "unclassified"
		}
		switch key {
		case "jsonrpc":
			if json.Unmarshal(value, &version) != nil {
				return "unclassified"
			}
		case "method":
			if json.Unmarshal(value, &method) != nil {
				return "unclassified"
			}
		}
	}
	if token, err := d.Token(); err != nil || token != json.Delim('}') {
		return "unclassified"
	}
	if _, err := d.Token(); err != io.EOF || version != "2.0" {
		return "unclassified"
	}
	switch method {
	case "initialize", "notifications/initialized", "tools/list", "tools/call":
		return method
	default:
		return "unclassified"
	}
}
