"""HTML fixtures for social media extraction tests."""

SOCIAL_FOOTER = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<footer>
  <div class="social-links">
    <a href="https://facebook.com/acmecorp">Facebook</a>
    <a href="https://twitter.com/acmecorp">Twitter</a>
    <a href="https://www.linkedin.com/company/acme-corp">LinkedIn</a>
    <a href="https://instagram.com/acmecorp">Instagram</a>
    <a href="https://youtube.com/c/acmecorp">YouTube</a>
  </div>
</footer>
</body></html>
"""

SOCIAL_WITH_SHARE_LINKS = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<div class="social-share">
  <a href="https://twitter.com/intent/tweet?text=Hello">Tweet This</a>
  <a href="https://www.facebook.com/sharer/sharer.php?u=example.com">Share</a>
  <a href="https://www.linkedin.com/shareArticle?url=example.com">Share on LinkedIn</a>
  <a href="https://pinterest.com/pin/create/button/?url=example.com">Pin It</a>
</div>
<footer>
  <a href="https://twitter.com/realacme">Follow us</a>
  <a href="https://facebook.com/realacme">Like us</a>
</footer>
</body></html>
"""

SOCIAL_X_DOMAIN = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<footer>
  <a href="https://x.com/acmecorp">X/Twitter</a>
  <a href="https://github.com/acmecorp">GitHub</a>
</footer>
</body></html>
"""

SOCIAL_ALL_PLATFORMS = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<footer>
  <div class="social">
    <a href="https://facebook.com/acme">FB</a>
    <a href="https://twitter.com/acme">TW</a>
    <a href="https://linkedin.com/company/acme">LI</a>
    <a href="https://instagram.com/acme">IG</a>
    <a href="https://youtube.com/acme">YT</a>
    <a href="https://tiktok.com/@acme">TT</a>
    <a href="https://github.com/acme">GH</a>
    <a href="https://pinterest.com/acme">PIN</a>
    <a href="https://threads.net/@acme">THR</a>
  </div>
</footer>
</body></html>
"""

SOCIAL_NO_PROFILES = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<footer><p>Copyright 2024</p></footer>
</body></html>
"""

SOCIAL_SCHEMA_ORG = """<!DOCTYPE html>
<html><head><title>Test</title>
<script type="application/ld+json">
{"@type": "Organization", "name": "Acme", "sameAs": ["https://twitter.com/acme", "https://facebook.com/acme", "https://linkedin.com/company/acme"]}
</script>
</head><body></body></html>
"""

SOCIAL_NAV_SECTION = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<nav class="social-nav">
  <a href="https://instagram.com/brandx">IG</a>
  <a href="https://tiktok.com/@brandx">TikTok</a>
</nav>
</body></html>
"""

SOCIAL_OTHER_PLATFORMS = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<footer>
  <a href="https://mastodon.social/@acme">Mastodon</a>
  <a href="https://discord.gg/acme">Discord</a>
  <a href="https://twitter.com/acme">Twitter</a>
</footer>
</body></html>
"""
