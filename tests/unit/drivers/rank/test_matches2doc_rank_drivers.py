from typing import Dict, List

from jina import Document
from jina.drivers.rank import Matches2DocRankDriver
from jina.executors.rankers import Match2DocRanker
from jina.executors.decorators import batching_multi_input
from jina.types.sets import DocumentSet


class MockMatches2DocRankDriver(Matches2DocRankDriver):
    def __init__(self, docs, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._docs = docs

    @property
    def exec_fn(self):
        return self._exec_fn

    @property
    def docs(self):
        return self._docs


class MockAbsoluteLengthRanker(Match2DocRanker):
    def __init__(self, *args, **kwargs):
        super().__init__(
            query_required_keys=('length',),
            match_required_keys=('length',),
            *args,
            **kwargs
        )

    @batching_multi_input(num_data=3)
    def score(
        self,
        old_match_scores: List[Dict],
        queries_metas: List[Dict],
        matches_metas: List[List[Dict]],
    ) -> List[List[float]]:
        return [
            [-abs(m['length'] - query_meta['length']) for m in match_meta]
            for query_meta, match_meta in zip(queries_metas, matches_metas)
        ]


def create_document_to_score():
    # doc: 1
    # |- matches: (id: 2, parent_id: 1, score.value: 2),
    # |- matches: (id: 3, parent_id: 1, score.value: 3),
    # |- matches: (id: 4, parent_id: 1, score.value: 4),
    # |- matches: (id: 5, parent_id: 1, score.value: 5),
    doc = Document()
    doc.id = '1' * 20
    doc.length = 5
    for match_id, match_score, match_length in [
        (2, 3, 16),
        (3, 6, 24),
        (4, 1, 8),
        (5, 8, 16),
    ]:
        with Document() as match:
            match.id = str(match_id) * match_length
            match.length = match_score
            match.score.value = match_score
            doc.matches.append(match)
    return doc


def test_chunk2doc_ranker_driver_mock_exec():
    doc = create_document_to_score()
    driver = MockMatches2DocRankDriver(DocumentSet([doc]))
    executor = MockAbsoluteLengthRanker()
    driver.attach(executor=executor, runtime=None)
    driver()
    assert len(doc.matches) == 4
    assert doc.matches[0].id == '3' * 24
    assert doc.matches[0].score.value == -1
    assert doc.matches[1].id == '2' * 16
    assert doc.matches[1].score.value == -2
    assert doc.matches[2].id == '5' * 16
    assert doc.matches[2].score.value == -3
    assert doc.matches[3].id == '4' * 8
    assert doc.matches[3].score.value == -4
    for match in doc.matches:
        assert match.score.ref_id == doc.id
        assert match.score.op_name == 'MockAbsoluteLengthRanker'
