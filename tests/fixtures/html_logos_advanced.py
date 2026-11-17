"""HTML fixtures for logo intelligence tests."""

LOGO_IN_HEADER = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<header>
  <a href="/"><img class="site-logo" src="https://example.com/logo.png" alt="Example Logo" width="200" height="50"></a>
  <nav><a href="/about">About</a></nav>
</header>
<main><p>Content</p></main>
</body></html>
"""

LOGO_SVG_IN_HEADER = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<header>
  <a href="/">
    <img class="logo" src="https://example.com/logo.svg" alt="SVG Logo" width="120" height="40">
  </a>
</header>
</body></html>
"""

LOGO_INLINE_SVG = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<header>
  <div class="logo-wrapper">
    <svg class="site-logo" viewBox="0 0 100 30"><rect width="100" height="30"/></svg>
  </div>
</header>
</body></html>
"""

LOGO_MULTIPLE_VARIANTS = """<!DOCTYPE html>
<html><head><title>Test</title>
<link rel="icon" sizes="16x16" href="/favicon-16.png">
<link rel="icon" sizes="32x32" href="/favicon-32.png">
<link rel="icon" sizes="192x192" href="/favicon-192.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch.png">
<meta property="og:image" content="https://example.com/og-image.jpg">
</head>
<body>
<header>
  <img id="main-logo" src="https://example.com/logo.png" alt="Main Logo" width="300" height="80">
</header>
</body></html>
"""

LOGO_SCHEMA_ORG = """<!DOCTYPE html>
<html><head><title>Test</title>
<script type="application/ld+json">
{"@type": "Organization", "name": "Test Corp", "logo": "https://example.com/schema-logo.svg"}
</script>
</head>
<body>
<header><img class="brand-logo" src="https://example.com/header-logo.png" alt="Brand"></header>
</body></html>
"""

LOGO_NO_HEADER = """<!DOCTYPE html>
<html><head><title>Test</title>
<link rel="icon" href="/favicon.ico">
<meta property="og:image" content="https://example.com/social.jpg">
</head>
<body>
<div class="nav-logo">
  <img src="https://example.com/nav-logo.png" alt="logo">
</div>
</body></html>
"""

LOGO_CENTER_POSITION = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<header>
  <div class="header-center">
    <img class="logo" src="https://example.com/center-logo.png" alt="Logo">
  </div>
</header>
</body></html>
"""

LOGO_RIGHT_POSITION = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<header>
  <div class="align-right">
    <img class="logo" src="https://example.com/right-logo.png" alt="Logo">
  </div>
</header>
</body></html>
"""

LOGO_SVG_PARENT_CLASS = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<header>
  <div class="logo">
    <svg viewBox="0 0 50 50"><circle cx="25" cy="25" r="20"/></svg>
  </div>
</header>
</body></html>
"""

NO_LOGOS = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body><p>No logos here.</p></body></html>
"""
