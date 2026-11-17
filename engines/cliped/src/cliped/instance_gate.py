"""Learns, from the verifier's answers, whether a frame's best box is the
target at all, so that a frame where the target is not among the detector's
boxes reports none rather than the next most like it.

Ranking only orders a frame's boxes against each other. The gate scores each
crop by how much more it is like the text prompt (e.g. "a person in a red
shirt") than like the plain class (e.g. "a person"): its margin. The target
scores higher than other instances of the class, but where to cut depends on
the prompt and the scene, so the cut is learned: the midpoint between the
mean margin of the crops the verifier said yes to and of those it said no to.
That separates the target from the class's other instances (other people),
apart from the tile learning, which compares crops with each other.

Offline, on two drone videos of three people, a cut learned from 4 answers
raised precision from 0.69 to 0.91 at the same recall (0.86) for a person
in a red shirt. Only a text prompt has a margin: an image's likeness to an
image is on another scale than to a text.
"""

import numpy as np

# Fewest yes and no answers each before the gate cuts
MIN_EACH = 1
# Answers kept of each kind; past it, the oldest is forgotten
MAX_ANSWERS = 128


class InstanceGate:
    """`t` is the prompt's vector and `c` the class prompt's."""

    def __init__(self, t, c, min_each=MIN_EACH, max_answers=MAX_ANSWERS):
        self.d = np.asarray(t, dtype=np.float64) - np.asarray(c, dtype=np.float64)
        self.min_each = min_each
        self.max_answers = max_answers
        self.yes = []
        self.no = []

    def margins(self, v):
        """How much more like the prompt than like the class each embedding
        of `v` is."""
        return np.atleast_2d(v) @ self.d

    def learn(self, embedding, match):
        """Adds the verifier's answer about the crop `embedding`."""
        answers = self.yes if match else self.no
        answers.append(float(self.margins(embedding)[0]))
        del answers[:-self.max_answers]

    @property
    def cutoff(self):
        """The least margin a box must exceed to be reported, or None while
        it can't cut: too few answers, or the yes's no higher than the no's."""
        if len(self.yes) < self.min_each or len(self.no) < self.min_each:
            return None
        yes, no = np.mean(self.yes), np.mean(self.no)
        return float((yes + no) / 2) if yes > no else None

    def passes(self, margin):
        cutoff = self.cutoff
        return cutoff is None or margin > cutoff
