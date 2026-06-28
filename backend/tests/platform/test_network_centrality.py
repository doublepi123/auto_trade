"""Tests for P347 network centrality module."""

from __future__ import annotations

import pytest

from app.platform.network_centrality import (
    NetworkCentralityResult,
    network_centrality_report,
)


class TestNetworkCentrality:
    def test_three_node_directed_graph(self):
        """Construct a 3-node directed graph and verify centrality measures."""
        adj = {
            "A": {"B": 1.0, "C": 0.5},
            "B": {"C": 2.0},
            "C": {"A": 1.0},
        }
        result = network_centrality_report(adj)
        assert isinstance(result, NetworkCentralityResult)
        # degree_centrality should have all 3 nodes
        assert set(result.degree_centrality.keys()) == {"A", "B", "C"}
        assert all(0.0 <= v <= 1.0 for v in result.degree_centrality.values())
        # betweenness_centrality should have all 3 nodes
        assert set(result.betweenness_centrality.keys()) == {"A", "B", "C"}
        # eigenvector_centrality should have all 3 nodes
        assert set(result.eigenvector_centrality.keys()) == {"A", "B", "C"}
        # pagerank should have all 3 nodes and sum to ~1
        assert set(result.pagerank.keys()) == {"A", "B", "C"}
        pagerank_sum = sum(result.pagerank.values())
        assert abs(pagerank_sum - 1.0) < 0.01
        # most_central_node should be one of the nodes
        assert result.most_central_node in {"A", "B", "C"}

    def test_simple_chain(self):
        """A → B → C chain graph."""
        adj = {
            "A": {"B": 1.0},
            "B": {"C": 1.0},
            "C": {},
        }
        result = network_centrality_report(adj)
        assert isinstance(result, NetworkCentralityResult)
        # B should have highest betweenness centrality (on the only path A→C)
        bc = result.betweenness_centrality
        assert bc["B"] >= bc["A"]
        assert bc["B"] >= bc["C"]
        # degree centrality: A has out=1 in=0, B has in=1 out=1, C has in=1 out=0
        dc = result.degree_centrality
        assert dc["B"] >= dc["A"]

    def test_star_graph(self):
        """Star graph: center connects to all leaves."""
        adj = {
            "center": {"A": 1.0, "B": 1.0, "C": 1.0},
            "A": {},
            "B": {},
            "C": {},
        }
        result = network_centrality_report(adj)
        assert result.most_central_node == "center"
        bc = result.betweenness_centrality
        # Directed star: no leaf-to-leaf paths exist, all betweenness is 0
        assert bc["center"] >= bc["A"]
        assert bc["center"] >= bc["B"]
        assert bc["center"] >= bc["C"]

    def test_complete_graph_equal_centrality(self):
        """Complete graph (all pairs connected) should have equal centrality."""
        adj = {
            "A": {"B": 1.0, "C": 1.0},
            "B": {"A": 1.0, "C": 1.0},
            "C": {"A": 1.0, "B": 1.0},
        }
        result = network_centrality_report(adj)
        dc = result.degree_centrality
        # All nodes have the same degree: 2 out + 2 in, normalized by (3-1)*2 = 4 → 0.5
        assert abs(dc["A"] - dc["B"]) < 1e-9
        assert abs(dc["A"] - dc["C"]) < 1e-9
        # Betweenness should also be equal
        bc = result.betweenness_centrality
        assert abs(bc["A"] - bc["B"]) < 1e-9

    def test_weighted_edges(self):
        """Weighted edges should affect eigenvector centrality and pagerank."""
        adj = {
            "A": {"B": 10.0, "C": 0.1},
            "B": {"C": 1.0},
            "C": {},
        }
        result = network_centrality_report(adj)
        # A has stronger connection to B, B connects to C
        # pagerank should sum to ~1
        assert abs(sum(result.pagerank.values()) - 1.0) < 0.01

    def test_to_dict(self):
        adj = {"A": {"B": 1.0}, "B": {}}
        result = network_centrality_report(adj)
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "degree_centrality" in d
        assert "betweenness_centrality" in d
        assert "eigenvector_centrality" in d
        assert "pagerank" in d
        assert "most_central_node" in d

    def test_empty_graph_raises(self):
        with pytest.raises(ValueError):
            network_centrality_report({})

    def test_non_dict_adjacency_raises(self):
        with pytest.raises(ValueError):
            network_centrality_report({"A": "not_a_dict"})  # type: ignore[dict-item]

    def test_non_finite_weight_raises(self):
        with pytest.raises(ValueError):
            network_centrality_report({"A": {"B": float("inf")}})
