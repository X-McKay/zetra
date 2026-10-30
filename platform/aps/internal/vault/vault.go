// Package vault seals subject tokens and exchanged tokens with AES-256-GCM,
// binding each ciphertext to its key (the session ID) as additional data (9.7).
package vault

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"strings"
)

type Vault struct{ aead cipher.AEAD }

func New(key []byte) (*Vault, error) {
	if len(key) != 32 {
		return nil, fmt.Errorf("vault key must be 32 bytes, got %d", len(key))
	}
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	aead, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	return &Vault{aead: aead}, nil
}

// LoadKey reads the base64 key from APS_VAULT_KEY or the file named by APS_VAULT_KEY_FILE.
func LoadKey() ([]byte, error) {
	v := os.Getenv("APS_VAULT_KEY")
	if f := os.Getenv("APS_VAULT_KEY_FILE"); v == "" && f != "" {
		b, err := os.ReadFile(f)
		if err != nil {
			return nil, err
		}
		v = string(b)
	}
	v = strings.TrimSpace(v)
	if v == "" {
		return nil, errors.New("APS_VAULT_KEY or APS_VAULT_KEY_FILE is required")
	}
	return base64.StdEncoding.DecodeString(v)
}

// Seal returns nonce || ciphertext with aad authenticated.
func (v *Vault) Seal(aad string, plaintext []byte) ([]byte, error) {
	nonce := make([]byte, v.aead.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, err
	}
	return v.aead.Seal(nonce, nonce, plaintext, []byte(aad)), nil
}

// Open reverses Seal; it fails if aad differs from the one used to seal.
func (v *Vault) Open(aad string, blob []byte) ([]byte, error) {
	n := v.aead.NonceSize()
	if len(blob) < n {
		return nil, errors.New("ciphertext too short")
	}
	return v.aead.Open(nil, blob[:n], blob[n:], []byte(aad))
}
