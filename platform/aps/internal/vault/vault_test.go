package vault

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/base64"
	"testing"
	"time"

	"github.com/X-McKay/zetra/platform/aps/internal/store"
)

func key(t *testing.T) []byte {
	k := make([]byte, 32)
	if _, err := rand.Read(k); err != nil {
		t.Fatal(err)
	}
	return k
}

func TestSealOpenWithAAD(t *testing.T) {
	v, err := New(key(t))
	if err != nil {
		t.Fatal(err)
	}
	blob, err := v.Seal("6f1d2c3e-0000-4000-8000-000000000001", []byte("subject token"))
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(blob, []byte("subject token")) {
		t.Fatal("plaintext visible in ciphertext")
	}
	got, err := v.Open("6f1d2c3e-0000-4000-8000-000000000001", blob)
	if err != nil || string(got) != "subject token" {
		t.Fatalf("open: %q %v", got, err)
	}
	if _, err := v.Open("6f1d2c3e-0000-4000-8000-000000000002", blob); err == nil {
		t.Fatal("open with wrong AAD (another session ID) succeeded")
	}
	other, _ := New(key(t))
	if _, err := other.Open("6f1d2c3e-0000-4000-8000-000000000001", blob); err == nil {
		t.Fatal("open with wrong key succeeded")
	}
}

func TestKeyLength(t *testing.T) {
	if _, err := New(make([]byte, 16)); err == nil {
		t.Fatal("16-byte key accepted")
	}
}

func TestLoadKeyFromEnv(t *testing.T) {
	k := key(t)
	t.Setenv("APS_VAULT_KEY", base64.StdEncoding.EncodeToString(k))
	got, err := LoadKey()
	if err != nil || !bytes.Equal(got, k) {
		t.Fatalf("LoadKey: %v", err)
	}
}

func TestEntriesRoundTripAndCreateOnce(t *testing.T) {
	v, _ := New(key(t))
	e := &Entries{Store: store.NewMemory(), Vault: v, Now: time.Now}
	ctx := context.Background()
	en := &Entry{AccessToken: "a", ExpiresAt: time.Now().Add(time.Hour), PrincipalSub: "alice"}
	if ok, err := e.Create(ctx, "s1", en); err != nil || !ok {
		t.Fatalf("create: %v %v", ok, err)
	}
	if ok, _ := e.Create(ctx, "s1", en); ok {
		t.Fatal("second create succeeded")
	}
	if p, _ := e.Principal(ctx, "s1"); p != "alice" {
		t.Fatalf("principal %q", p)
	}
	if ok, _ := e.Usable(ctx, "s1"); !ok {
		t.Fatal("expected usable")
	}
	en.Expired = true
	_ = e.Save(ctx, "s1", en)
	if ok, _ := e.Usable(ctx, "s1"); ok {
		t.Fatal("expired entry usable")
	}
}
