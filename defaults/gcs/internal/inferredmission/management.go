package inferredmission

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"
)

type runPatch struct {
	Name     *string `json:"display_name"`
	Archived *bool   `json:"archived"`
}

var errArchiveActive = errors.New("Pause or stop this analysis before archiving it.")

func (s *Service) manageRun(w http.ResponseWriter, req *http.Request, r *run) {
	var patch *runPatch
	dec := json.NewDecoder(http.MaxBytesReader(w, req.Body, 4<<10))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&patch); err != nil || patch == nil {
		failHTTP(w, http.StatusBadRequest, "Invalid analysis session update.")
		return
	}
	if err := dec.Decode(new(json.RawMessage)); !errors.Is(err, io.EOF) {
		failHTTP(w, http.StatusBadRequest, "Invalid analysis session update.")
		return
	}
	if patch.Name != nil {
		*patch.Name = strings.TrimSpace(*patch.Name)
		if count := utf8.RuneCountInString(*patch.Name); count < 1 || count > 120 {
			failHTTP(w, http.StatusUnprocessableEntity, "Display name must contain 1 to 120 characters.")
			return
		}
	}
	meta, err := r.patch(*patch)
	if err != nil {
		if errors.Is(err, errArchiveActive) {
			failHTTP(w, http.StatusConflict, err.Error())
			return
		}
		s.log.Warn().Err(err).Msg("could not persist analysis session update")
		failHTTP(w, http.StatusInternalServerError, "Could not save the analysis session update.")
		return
	}
	reply(w, http.StatusOK, meta)
}

func (r *run) patch(patch runPatch) (session, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	meta := r.meta
	if patch.Name != nil {
		meta.Name = *patch.Name
	}
	if patch.Archived != nil && *patch.Archived != (meta.Archived != nil) {
		if *patch.Archived {
			if r.running || active(meta.Status) {
				return session{}, errArchiveActive
			}
			at := seconds(time.Now())
			meta.Archived = &at
		} else {
			meta.Archived = nil
		}
	}
	if meta.Name != r.meta.Name || meta.Archived != r.meta.Archived {
		if err := r.setSession(meta); err != nil {
			return session{}, err
		}
	}
	return r.meta, nil
}

func jsonDownloadDisposition(meta session, index int, artifact string) string {
	source := meta.Name
	if source == "" && meta.Video != nil {
		source = strings.TrimSuffix(meta.Video.Name, path.Ext(meta.Video.Name))
	}
	if source == "" {
		source = meta.Vehicle
	}
	name := downloadPrefix(source, false)
	fallback := downloadPrefix(name, true)
	stamp := time.UnixMilli(int64(meta.Created * 1000)).UTC().Format("20060102T150405.000Z")
	suffix := fmt.Sprintf("_%s_section-%06d_%s", stamp, index, artifact)
	result := fmt.Sprintf("attachment; filename=%q", fallback+suffix)
	if name != fallback {
		result += "; filename*=UTF-8''" + url.PathEscape(name+suffix)
	}
	return result
}

// downloadPrefix keeps letters and numbers from the original label while
// keeping the final file name comfortably within common filesystem limits.
func downloadPrefix(label string, ascii bool) string {
	const maxBytes = 180
	var out strings.Builder
	gap := false
	for _, r := range label {
		if (ascii && r > unicode.MaxASCII) ||
			(!(unicode.IsLetter(r) || unicode.IsNumber(r) || unicode.IsMark(r)) && r != '.' && r != '-' && r != '_') {
			gap = out.Len() > 0
			continue
		}
		need := utf8.RuneLen(r)
		if gap {
			need++
		}
		if out.Len()+need > maxBytes {
			break
		}
		if gap {
			out.WriteByte('-')
			gap = false
		}
		out.WriteRune(r)
	}
	if name := strings.Trim(out.String(), ".-_"); name != "" {
		return name
	}
	return "analysis-run"
}
