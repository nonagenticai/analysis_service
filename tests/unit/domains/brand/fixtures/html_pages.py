"""HTML page fixtures for testing."""

MINIMAL_HTML = """<!DOCTYPE html>
<html><head><title>Test Page</title></head><body></body></html>
"""

HTML_WITH_OG_TAGS = """<!DOCTYPE html>
<html><head>
<title>Acme Corp | Home</title>
<meta property="og:site_name" content="Acme Corporation">
<meta property="og:image" content="https://acme.com/og-image.png">
<meta name="twitter:image" content="https://acme.com/twitter.png">
</head><body></body></html>
"""

HTML_WITH_THEME_COLOR = """<!DOCTYPE html>
<html><head>
<title>Branded Page</title>
<meta name="theme-color" content="#FF5733">
<meta name="msapplication-TileColor" content="#da532c">
</head><body></body></html>
"""

HTML_WITH_CSS_VARS = """<!DOCTYPE html>
<html><head>
<title>CSS Vars Site</title>
<style>
:root {
    --primary-color: #3498db;
    --brand-color: #e74c3c;
    --accent_color: #2ecc71;
}
body { font-family: 'Roboto', sans-serif; }
h1 { font-family: 'Playfair Display', serif; }
</style>
</head><body></body></html>
"""

HTML_WITH_LOGOS = """<!DOCTYPE html>
<html><head>
<title>Logo Site</title>
<link rel="icon" sizes="32x32" href="/favicon-32x32.png">
<link rel="icon" sizes="16x16" href="/favicon-16x16.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<meta property="og:image" content="https://example.com/og.png">
</head><body>
<img class="logo" src="https://example.com/logo.png" alt="Site Logo">
</body></html>
"""

HTML_WITH_ALL_SIGNALS = """<!DOCTYPE html>
<html><head>
<title>Full Brand | Home</title>
<meta name="theme-color" content="#FF5733">
<meta property="og:site_name" content="Full Brand Inc">
<meta property="og:image" content="https://fullbrand.com/og.png">
<meta name="twitter:image" content="https://fullbrand.com/tw.png">
<link rel="icon" sizes="32x32" href="/favicon-32.png">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch.png">
<link rel="manifest" href="/manifest.json">
<link href="https://fonts.googleapis.com/css2?family=Inter&display=swap" rel="stylesheet">
<style>
:root { --primary-color: #FF5733; --brand-color: #C70039; }
body { font-family: 'Inter', sans-serif; }
</style>
<script type="application/ld+json">
{"@type": "Organization", "name": "Full Brand Inc", "url": "https://fullbrand.com", "logo": "https://fullbrand.com/logo.png"}
</script>
</head><body>
<img class="site-logo" src="https://fullbrand.com/logo.png" alt="Full Brand Logo">
</body></html>
"""

HTML_WITH_SCHEMA_ORG = """<!DOCTYPE html>
<html><head>
<title>Schema Site</title>
<script type="application/ld+json">
{"@type": "Organization", "name": "Schema Corp", "url": "https://schema.com", "logo": {"@type": "ImageObject", "url": "https://schema.com/logo.png"}, "sameAs": ["https://twitter.com/schema", "https://facebook.com/schema"]}
</script>
</head><body></body></html>
"""

HTML_WITH_GOOGLE_FONTS = """<!DOCTYPE html>
<html><head>
<title>Font Site</title>
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;700&family=Open+Sans&display=swap" rel="stylesheet">
</head><body></body></html>
"""
