import unittest

from backend.cluster_manager import parse_cluster_nodes, parse_proxy_targets


class ClusterManagerTests(unittest.TestCase):
    def test_parse_cluster_nodes(self):
        nodes = parse_cluster_nodes(
            "warp-1=http://warp-1:8000,warp-2=http://warp-2:8000"
        )

        self.assertEqual([node.id for node in nodes], ["warp-1", "warp-2"])
        self.assertEqual(nodes[0].base_url, "http://warp-1:8000")

    def test_parse_proxy_targets_from_env(self):
        targets = parse_proxy_targets(
            "warp-1=warp-1:1080,warp-2=warp-2:1080",
            default_port=1080,
        )

        self.assertEqual([target.label for target in targets], ["warp-1", "warp-2"])
        self.assertEqual(targets[1].host, "warp-2")
        self.assertEqual(targets[1].port, 1080)

    def test_parse_proxy_targets_from_nodes(self):
        nodes = parse_cluster_nodes("warp-1=http://warp-1:8000")
        targets = parse_proxy_targets(None, default_port=8080, nodes=nodes)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].label, "warp-1")
        self.assertEqual(targets[0].host, "warp-1")
        self.assertEqual(targets[0].port, 8080)


if __name__ == "__main__":
    unittest.main()
