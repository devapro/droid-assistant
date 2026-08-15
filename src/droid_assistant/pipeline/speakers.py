"""Online speaker clustering for the live latency modes.

Two paths, for two different problems:

* **Batch mode** runs the diarizer over the whole session at once. That is the
  accurate answer, because clustering benefits from seeing everything.
* **Live and Balanced** cannot wait for the session to end. Each finalised
  utterance is embedded and matched against running centroids — cheap,
  incremental, and stable within the session, which is what FR-DIA-1 asks for.

The live path is knowingly weaker than the offline one. It is also the only
thing that can attribute a speaker to a line the moment it appears, which is
what makes a live transcript readable rather than a wall of text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ..domain import Embedding

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Cluster:
    index: int
    centroid: Embedding
    count: int = 1

    def absorb(self, embedding: Embedding) -> None:
        """Running mean. Weighting by count keeps a long-standing speaker's
        centroid from being dragged by one noisy utterance."""
        self.count += 1
        updated = self.centroid + (embedding - self.centroid) / self.count
        norm = float(np.linalg.norm(updated))
        if norm > 0:
            updated = updated / norm
        # numpy widens to float64 on mixed arithmetic; the column and every
        # consumer expect float32.
        self.centroid = updated.astype(np.float32)


@dataclass
class OnlineSpeakerClusterer:
    """Greedy nearest-centroid assignment with a similarity threshold.

    `threshold` is the knob that trades the two failure modes against each
    other: too low and two people merge into one label; too high and one person
    fragments across several. The default matches the sherpa-onnx clustering
    default so the live and offline paths do not disagree wildly.
    """

    threshold: float = 0.5
    min_speakers: int | None = None
    max_speakers: int | None = None
    clusters: list[Cluster] = field(default_factory=list)

    def assign(self, embedding: Embedding | None) -> int | None:
        """Return a stable speaker index, or None when there is nothing to go on."""
        if embedding is None or embedding.size == 0:
            return None
        vec = _unit(embedding)

        if not self.clusters:
            self.clusters.append(Cluster(index=0, centroid=vec))
            return 0

        similarities = [float(np.dot(vec, c.centroid)) for c in self.clusters]
        best = int(np.argmax(similarities))
        best_similarity = similarities[best]

        at_capacity = self.max_speakers is not None and len(self.clusters) >= self.max_speakers
        if best_similarity >= self.threshold or at_capacity:
            # FR-DIA-3: with an exact count pinned, never invent a further
            # speaker — force the nearest existing one instead.
            self.clusters[best].absorb(vec)
            return self.clusters[best].index

        cluster = Cluster(index=len(self.clusters), centroid=vec)
        self.clusters.append(cluster)
        return cluster.index

    @property
    def speaker_count(self) -> int:
        return len(self.clusters)

    def centroid(self, index: int) -> Embedding | None:
        for cluster in self.clusters:
            if cluster.index == index:
                return cluster.centroid
        return None


def _unit(vec: Embedding) -> Embedding:
    norm = float(np.linalg.norm(vec))
    return vec.astype(np.float32) if norm == 0 else (vec / norm).astype(np.float32)


def relabel_by_first_appearance(assignments: list[int]) -> dict[int, int]:
    """Map raw cluster ids to appearance order.

    The palette is indexed by speaker number (FR-UI-16), so "Speaker 1" should
    be whoever spoke first, not whichever cluster the algorithm happened to
    create first.
    """
    order: dict[int, int] = {}
    for raw in assignments:
        if raw not in order:
            order[raw] = len(order)
    return order
