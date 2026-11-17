"""Tests for repo_detector module."""

from src.domains.critique.services.repo_detector import REPO_PATTERN, RepoDetection, detect_repo_url


class TestRepoPattern:
    """Test the REPO_PATTERN regex."""

    def test_github_url(self):
        match = REPO_PATTERN.search("https://github.com/org/repo")
        assert match is not None
        assert match.group(0) == "https://github.com/org/repo"

    def test_gitlab_url(self):
        match = REPO_PATTERN.search("https://gitlab.com/org/repo")
        assert match is not None

    def test_bitbucket_url(self):
        match = REPO_PATTERN.search("https://bitbucket.org/org/repo")
        assert match is not None

    def test_no_match(self):
        match = REPO_PATTERN.search("https://example.com/page")
        assert match is None

    def test_www_prefix(self):
        match = REPO_PATTERN.search("https://www.github.com/org/repo")
        assert match is not None


class TestRepoDetection:
    """Test RepoDetection dataclass."""

    def test_defaults(self):
        d = RepoDetection()
        assert d.url is None
        assert d.confidence == "none"
        assert d.source == ""
        assert d.candidates == []

    def test_with_values(self):
        d = RepoDetection(
            url="https://github.com/org/repo",
            confidence="high",
            source="meta tag",
            candidates=["https://github.com/org/repo"],
        )
        assert d.url == "https://github.com/org/repo"
        assert d.confidence == "high"


class TestDetectRepoUrl:
    """Test detect_repo_url function."""

    def test_no_repo(self):
        html = "<html><body><p>Hello world</p></body></html>"
        result = detect_repo_url(html)
        assert result.url is None
        assert result.confidence == "none"

    def test_meta_tag_detection(self):
        html = """
        <html><head>
            <meta name="repo" content="https://github.com/myorg/myrepo">
        </head><body></body></html>
        """
        result = detect_repo_url(html)
        assert result.url == "https://github.com/myorg/myrepo"
        assert result.confidence == "high"
        assert result.source == "meta tag"

    def test_anchor_text_detection(self):
        html = """
        <html><body>
            <a href="https://github.com/myorg/myrepo">View on GitHub</a>
        </body></html>
        """
        result = detect_repo_url(html)
        assert result.url == "https://github.com/myorg/myrepo"
        assert result.confidence == "high"
        assert result.source == "anchor text"

    def test_footer_nav_detection(self):
        html = """
        <html><body>
            <footer>
                <a href="https://github.com/org/repo">GitHub</a>
            </footer>
        </body></html>
        """
        result = detect_repo_url(html)
        assert result.url is not None
        # "GitHub" in anchor text makes it high confidence anchor text match
        assert result.url == "https://github.com/org/repo"

    def test_link_tag_detection(self):
        html = """
        <html><head>
            <link rel="source" href="https://github.com/org/repo">
        </head><body></body></html>
        """
        result = detect_repo_url(html)
        assert result.url == "https://github.com/org/repo"

    def test_body_link_low_confidence(self):
        html = """
        <html><body>
            <p>Check out <a href="https://github.com/org/repo">our page</a></p>
        </body></html>
        """
        result = detect_repo_url(html)
        assert result.url == "https://github.com/org/repo"
        assert result.confidence == "low"

    def test_multiple_candidates_sorted(self):
        html = """
        <html><head>
            <meta name="repo" content="https://github.com/org/meta-repo">
        </head><body>
            <a href="https://github.com/org/body-repo">Check source</a>
        </body></html>
        """
        result = detect_repo_url(html)
        # Meta tag is high confidence, should win
        assert result.url == "https://github.com/org/meta-repo"
        assert len(result.candidates) >= 2

    def test_source_code_anchor_text(self):
        html = """
        <html><body>
            <a href="https://github.com/org/repo">Source code</a>
        </body></html>
        """
        result = detect_repo_url(html)
        assert result.confidence == "high"
        assert result.source == "anchor text"
