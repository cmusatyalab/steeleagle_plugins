// Package aicredentials stores private GCS credentials outside daemon config.
package aicredentials

import (
	"crypto/rand"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
)

const (
	openAI     = "openai"
	anthropic  = "anthropic"
	maxKeySize = 4096
	maxFile    = 64 << 10
)

type keys struct {
	OpenAI    string `json:"openai,omitempty"`
	Anthropic string `json:"anthropic,omitempty"`
}

func (k *keys) forProvider(provider string) *string {
	switch provider {
	case openAI:
		return &k.OpenAI
	case anthropic:
		return &k.Anthropic
	default:
		return nil
	}
}

// Store gives saved keys precedence over the environment at GCS startup.
type Store struct {
	path  string
	env   keys
	mu    sync.RWMutex
	saved keys
}

// Open reads a credential file or starts with an empty store if it does not
// exist. An empty path allows only the environment fallback.
func Open(path string) (*Store, error) {
	s := &Store{env: keys{
		OpenAI:    os.Getenv("OPENAI_API_KEY"),
		Anthropic: os.Getenv("ANTHROPIC_API_KEY"),
	}}
	if path == "" {
		return s, nil
	}
	path, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return nil, err
	}
	root, err := openPrivateParent(path)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	if err := readSaved(root, filepath.Base(path), &s.saved); err != nil {
		return nil, err
	}
	s.path = path
	return s, nil
}

// Key returns the current key for a provider. Unknown providers have no key.
func (s *Store) Key(provider string) string {
	s.mu.RLock()
	defer s.mu.RUnlock()
	saved := s.saved.forProvider(provider)
	if saved == nil {
		return ""
	}
	if *saved != "" {
		return *saved
	}
	return *s.env.forProvider(provider)
}

func (s *Store) replace(provider, key string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	next := s.saved
	previous := next.forProvider(provider)
	if *previous == key {
		return nil
	}
	*previous = key
	if err := writeSaved(s.path, next); err != nil {
		return err
	}
	s.saved = next
	return nil
}

func openPrivateParent(path string) (*os.Root, error) {
	parent := filepath.Dir(path)
	info, err := os.Lstat(parent)
	if err != nil {
		return nil, err
	}
	if !info.IsDir() || !ownedAndPrivate(info) {
		return nil, errors.New("credential directory must be private and owned by the GCS user")
	}
	root, err := os.OpenRoot(parent)
	if err != nil {
		return nil, err
	}
	opened, err := root.Stat(".")
	if err != nil || !os.SameFile(info, opened) || !ownedAndPrivate(opened) {
		root.Close()
		return nil, errors.New("credential directory changed while opening it")
	}
	return root, nil
}

func ownedAndPrivate(info os.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Uid == uint32(os.Geteuid()) && info.Mode().Perm()&0o077 == 0
}

func privateFile(root *os.Root, name string) (os.FileInfo, error) {
	info, err := root.Lstat(name)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || !ownedAndPrivate(info) {
		return nil, errors.New("credential file must be a private regular file owned by the GCS user")
	}
	return info, nil
}

func readSaved(root *os.Root, name string, saved *keys) error {
	info, err := privateFile(root, name)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	dir, err := root.Open(".")
	if err != nil {
		return err
	}
	defer dir.Close()
	// Keep the open relative to the verified directory and refuse to follow
	// the file if it changed to a symlink or FIFO after Lstat.
	fd, err := syscall.Openat(int(dir.Fd()), name, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		return err
	}
	f := os.NewFile(uintptr(fd), name)
	defer f.Close()
	opened, err := f.Stat()
	if err != nil || !os.SameFile(info, opened) || !ownedAndPrivate(opened) {
		return errors.New("credential file changed while opening it")
	}
	data, err := io.ReadAll(io.LimitReader(f, maxFile+1))
	if err != nil {
		return err
	}
	if len(data) > maxFile || json.Unmarshal(data, saved) != nil ||
		(saved.OpenAI != "" && !validKey(saved.OpenAI)) || (saved.Anthropic != "" && !validKey(saved.Anthropic)) {
		return errors.New("credential file has invalid contents")
	}
	return nil
}

func writeSaved(path string, saved keys) error {
	root, err := openPrivateParent(path)
	if err != nil {
		return err
	}
	defer root.Close()
	name := filepath.Base(path)
	if _, err := privateFile(root, name); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	temp := ".ai-credentials-" + rand.Text()
	f, err := root.OpenFile(temp, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return err
	}
	defer root.Remove(temp)
	defer f.Close()
	if err := f.Chmod(0o600); err != nil {
		return err
	}
	if err := json.NewEncoder(f).Encode(saved); err != nil {
		return err
	}
	if err := f.Sync(); err != nil {
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	return root.Rename(temp, name)
}

func validKey(key string) bool {
	// Provider keys are HTTP header values. Require a nonempty visible ASCII
	// token without imposing any provider-specific prefix.
	return key != "" && len(key) <= maxKeySize && strings.IndexFunc(key, func(c rune) bool {
		return c < '!' || c > '~'
	}) < 0
}
