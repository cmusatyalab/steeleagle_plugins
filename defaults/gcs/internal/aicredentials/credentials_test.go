package aicredentials

import (
	"crypto/tls"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

const (
	fakeEnvironment = "fixture-env-key"
	fakeOpenAI      = "fixture-openai-key"
	fakeAnthropic   = "fixture-anthropic-key"
)

func TestCredentialPersistence(t *testing.T) {
	store, path := testStore(t, fakeEnvironment, "")
	mux := credentialMux(store)
	got := readStatus(t, mux, httptest.NewRequest(http.MethodGet, api, nil), false)
	if got.CanManage || got.OpenAI != (providerStatus{true, "environment"}) || got.Anthropic != (providerStatus{false, "missing"}) {
		t.Fatalf("unexpected initial status: %+v", got)
	}
	got = readStatus(t, mux, loopback(http.MethodGet, api, ""), true)
	if !got.CanManage || store.Key(openAI) != fakeEnvironment {
		t.Fatal("loopback status or environment fallback unavailable")
	}
	got = readStatus(t, mux, loopback(http.MethodPut, api+"/openai", `{"key":"  `+fakeOpenAI+`\n"}`), true)
	if !got.CanManage || got.OpenAI != (providerStatus{true, "saved"}) || store.Key(openAI) != fakeOpenAI {
		t.Fatal("saved key did not override the environment")
	}
	got = readStatus(t, mux, loopback(http.MethodPut, api+"/anthropic", `{"key":"`+fakeAnthropic+`"}`), true)
	if got.OpenAI.Source != "saved" || got.Anthropic.Source != "saved" || store.Key(anthropic) != fakeAnthropic {
		t.Fatal("saving the second provider did not preserve the first")
	}
	for name, mode := range map[string]os.FileMode{path: 0o600, filepath.Dir(path): 0o700} {
		info, err := os.Stat(name)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != mode {
			t.Errorf("%s permissions = %o, want %o", filepath.Base(name), info.Mode().Perm(), mode)
		}
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), fakeEnvironment) {
		t.Fatal("the environment key was written to storage")
	}
	loaded, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Key(openAI) != fakeOpenAI || loaded.Key(anthropic) != fakeAnthropic {
		t.Fatal("saved keys unavailable after reopening")
	}
	mux = credentialMux(loaded)
	for i := 0; i < 2; i++ {
		got = readStatus(t, mux, loopback(http.MethodDelete, api+"/openai", ""), true)
		if got.OpenAI.Source != "environment" || got.Anthropic.Source != "saved" {
			t.Fatal("clearing a saved key did not restore the environment fallback")
		}
	}
	readStatus(t, mux, loopback(http.MethodDelete, api+"/anthropic", ""), true)
	loaded, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Key(openAI) != fakeEnvironment || loaded.Key(anthropic) != "" {
		t.Fatal("cleared keys were restored after reopening")
	}
}

func TestCredentialManagementRequiresDirectSameOriginLoopback(t *testing.T) {
	store, _ := testStore(t, "", "")
	mux := credentialMux(store)
	for name, change := range map[string]func(*http.Request){
		"remote":       func(r *http.Request) { r.RemoteAddr = "192.0.2.44:60214" },
		"unix peer":    func(r *http.Request) { r.RemoteAddr = "@credential-proxy" },
		"foreign host": func(r *http.Request) { r.Host = "attacker.example:8002"; r.Header.Set("Origin", "http://"+r.Host) },
		"foreign IP":   func(r *http.Request) { r.Host = "192.0.2.44:8002"; r.Header.Set("Origin", "http://"+r.Host) },
		"cross-origin": func(r *http.Request) { r.Header.Set("Origin", "https://attacker.example") },
		"other port":   func(r *http.Request) { r.Header.Set("Origin", "http://127.0.0.1:8003") },
		"other scheme": func(r *http.Request) { r.Header.Set("Origin", "https://"+r.Host) },
		"duplicated":   func(r *http.Request) { r.Header.Add("Origin", "http://"+r.Host) },
		"null origin":  func(r *http.Request) { r.Header.Set("Origin", "null") },
		"forwarded":    func(r *http.Request) { r.Header.Set("Forwarded", "for=127.0.0.1") },
		"xff":          func(r *http.Request) { r.Header.Set("X-Forwarded-For", "127.0.0.1") },
		"forward host": func(r *http.Request) { r.Header.Set("X-Forwarded-Host", r.Host) },
		"proxy via":    func(r *http.Request) { r.Header.Set("Via", "1.1 localhost") },
		"proxy header": func(r *http.Request) { r.Header.Set("X-Real-IP", "127.0.0.1") },
	} {
		t.Run(name, func(t *testing.T) {
			req := loopback(http.MethodPut, api+"/openai", `{"key":"`+fakeOpenAI+`"}`)
			change(req)
			serve(t, mux, req, http.StatusForbidden)
			if store.Key(openAI) != "" {
				t.Fatal("a denied request stored a key")
			}
			statusRequest := loopback(http.MethodGet, api, "")
			statusRequest.Header.Set("Origin", "http://"+statusRequest.Host)
			change(statusRequest)
			if readStatus(t, mux, statusRequest, false).CanManage {
				t.Fatal("status enabled management for an untrusted connection")
			}
		})
	}
	for _, method := range []string{http.MethodPut, http.MethodDelete} {
		req := loopback(method, api+"/openai", `{"key":"`+fakeOpenAI+`"}`)
		req.Header.Del("Origin")
		serve(t, mux, req, http.StatusForbidden)
	}
	for _, host := range []string{"127.0.0.1:8002", "localhost:8002", "[::1]:8002"} {
		req := loopback(http.MethodPut, api+"/openai", `{"key":"`+fakeOpenAI+`"}`)
		req.RemoteAddr = "[::1]:51001"
		req.Host = host
		req.Header.Set("Origin", "https://"+host)
		req.TLS = &tls.ConnectionState{}
		if !readStatus(t, mux, req, true).CanManage {
			t.Errorf("same-origin requests denied for %s", host)
		}
	}
}

func TestInvalidCredentialRequestsKeepCurrentKey(t *testing.T) {
	store, path := testStore(t, "", "")
	mux := credentialMux(store)
	readStatus(t, mux, loopback(http.MethodPut, api+"/openai", `{"key":"`+fakeOpenAI+`"}`), true)
	for _, body := range []string{
		"", `{}`, `{"key":null}`, `{"key":123}`, `{"key":" "}`, `{"key":"a b"}`,
		`{"key":"a\nb"}`, `{"key":"` + fakeAnthropic + `", "extra":true}`,
		`{"key":"` + fakeAnthropic + `"} {}`, `{"key":"` + strings.Repeat("a", maxKeySize+1) + `"}`,
	} {
		serve(t, mux, loopback(http.MethodPut, api+"/openai", body), http.StatusBadRequest)
	}
	serve(t, mux, loopback(http.MethodPut, api+"/other", `{"key":"`+fakeAnthropic+`"}`), http.StatusNotFound)
	serve(t, mux, loopback(http.MethodPut, api+"/openai", strings.Repeat(" ", maxBody+1)), http.StatusRequestEntityTooLarge)
	if err := os.Chmod(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	serve(t, mux, loopback(http.MethodPut, api+"/openai", `{"key":"`+fakeAnthropic+`"}`), http.StatusInternalServerError)
	if err := os.Chmod(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	loaded, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if store.Key(openAI) != fakeOpenAI || loaded.Key(openAI) != fakeOpenAI {
		t.Fatal("a failed write changed the stored key")
	}
}

func TestConcurrentCredentialWritesPreserveBothProviders(t *testing.T) {
	store, path := testStore(t, "", "")
	mux := credentialMux(store)
	var writers sync.WaitGroup
	for provider, key := range map[string]string{openAI: fakeOpenAI, anthropic: fakeAnthropic} {
		writers.Go(func() {
			serve(t, mux, loopback(http.MethodPut, api+"/"+provider, `{"key":"`+key+`"}`), http.StatusOK)
		})
	}
	writers.Wait()
	loaded, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Key(openAI) != fakeOpenAI || loaded.Key(anthropic) != fakeAnthropic {
		t.Fatal("concurrent saves lost a key")
	}
}

func TestOpenRejectsUnsafeCredentialFiles(t *testing.T) {
	t.Setenv("OPENAI_API_KEY", "")
	t.Setenv("ANTHROPIC_API_KEY", "")
	parent := filepath.Join(t.TempDir(), "private")
	if err := os.Mkdir(parent, 0o700); err != nil {
		t.Fatal(err)
	}
	private := filepath.Join(parent, "private.json")
	public := filepath.Join(parent, "public.json")
	data := []byte(`{"openai":"` + fakeOpenAI + `"}`)
	for _, path := range []string{private, public} {
		if err := os.WriteFile(path, data, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Chmod(public, 0o644); err != nil {
		t.Fatal(err)
	}
	linkedFile := filepath.Join(parent, "link.json")
	linkedParent := filepath.Join(filepath.Dir(parent), "linked-parent")
	for link, target := range map[string]string{linkedFile: private, linkedParent: parent} {
		if err := os.Symlink(target, link); err != nil {
			t.Fatal(err)
		}
	}
	for _, path := range []string{public, linkedFile, filepath.Join(linkedParent, "private.json")} {
		if _, err := Open(path); err == nil {
			t.Errorf("accepted unsafe storage %s", path)
		} else if strings.Contains(err.Error(), fakeOpenAI) {
			t.Error("storage error leaked the credential")
		}
	}
}

func TestEnvironmentOnlyStore(t *testing.T) {
	t.Setenv("OPENAI_API_KEY", fakeEnvironment)
	t.Setenv("ANTHROPIC_API_KEY", fakeAnthropic)
	store, err := Open("")
	if err != nil {
		t.Fatal(err)
	}
	mux := credentialMux(store)
	status := readStatus(t, mux, loopback(http.MethodGet, api, ""), false)
	if status.CanManage || status.OpenAI.Source != "environment" || status.Anthropic.Source != "environment" {
		t.Fatal("unexpected environment-only status")
	}
	serve(t, mux, loopback(http.MethodPut, api+"/openai", `{"key":"`+fakeOpenAI+`"}`), http.StatusForbidden)
	serve(t, mux, loopback(http.MethodDelete, api+"/anthropic", ""), http.StatusForbidden)
	if store.Key(openAI) != fakeEnvironment || store.Key(anthropic) != fakeAnthropic || store.Key("other") != "" {
		t.Fatal("environment-only store returned the wrong keys")
	}
}

func testStore(t *testing.T, openAIKey, anthropicKey string) (*Store, string) {
	t.Helper()
	t.Setenv("OPENAI_API_KEY", openAIKey)
	t.Setenv("ANTHROPIC_API_KEY", anthropicKey)
	path := filepath.Join(t.TempDir(), "private", "keys.json")
	store, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	return store, path
}

func credentialMux(store *Store) *http.ServeMux {
	mux := http.NewServeMux()
	store.Register(mux)
	return mux
}

func loopback(method, path, body string) *http.Request {
	r := httptest.NewRequest(method, path, strings.NewReader(body))
	r.RemoteAddr = "127.0.0.1:51001"
	r.Host = "127.0.0.1:8002"
	if method != http.MethodGet {
		r.Header.Set("Origin", "http://"+r.Host)
	}
	return r
}

func readStatus(t *testing.T, mux *http.ServeMux, req *http.Request, canManage bool) credentialStatus {
	t.Helper()
	res := serve(t, mux, req, http.StatusOK)
	var status credentialStatus
	if err := json.Unmarshal(res.Body.Bytes(), &status); err != nil {
		t.Fatal(err)
	}
	if status.CanManage != canManage {
		t.Errorf("can_manage = %t, want %t", status.CanManage, canManage)
	}
	return status
}

func serve(t *testing.T, mux *http.ServeMux, req *http.Request, code int) *httptest.ResponseRecorder {
	t.Helper()
	res := httptest.NewRecorder()
	mux.ServeHTTP(res, req)
	if res.Code != code {
		t.Errorf("%s %s response status = %d, want %d", req.Method, req.URL, res.Code, code)
	}
	if res.Header().Get("Cache-Control") != "no-store" {
		t.Error("credential response can be cached")
	}
	for _, key := range []string{fakeEnvironment, fakeOpenAI, fakeAnthropic} {
		if strings.Contains(res.Body.String(), key) {
			t.Error("credential response exposed a raw key")
		}
	}
	return res
}
