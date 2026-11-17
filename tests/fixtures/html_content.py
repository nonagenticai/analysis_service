"""HTML fixtures for content analysis tests."""

HERO_TAGLINE = """<!DOCTYPE html>
<html><head><title>Acme Corp</title>
<meta name="description" content="Acme builds tools for modern teams.">
<meta property="og:description" content="Acme - Tools for modern teams">
</head><body>
<header>
  <nav><h2>Menu</h2></nav>
</header>
<section class="hero">
  <h1>Build better products, faster</h1>
  <p>The platform for modern development teams.</p>
</section>
<section>
  <h2>Features</h2>
  <h3>Real-time collaboration</h3>
  <h3>Cloud deployment</h3>
</section>
<footer><h3>Company</h3></footer>
</body></html>
"""

TECHNICAL_CONTENT = """<!DOCTYPE html>
<html><head><title>DevTools API</title></head>
<body>
<h1>Developer Documentation</h1>
<p>Our API provides a comprehensive SDK for integration with your infrastructure.
Deploy to any platform using our framework. Access the documentation for code examples.</p>
<h2>API Reference</h2>
<h3>Authentication</h3>
<p>Use the developer SDK to authenticate with our platform infrastructure.</p>
</body></html>
"""

CASUAL_CONTENT = """<!DOCTYPE html>
<html><head><title>Fun App</title></head>
<body>
<div class="hero">
  <h1>Awesome tools for awesome people</h1>
</div>
<p>Hey there! We love making things simple and fun.
Our amazing tools are easy to use and totally free to try.
Cool features that make your life easier.</p>
</body></html>
"""

CORPORATE_CONTENT = """<!DOCTYPE html>
<html><head><title>GlobalCorp</title></head>
<body>
<h1>Driving Stakeholder Value</h1>
<p>Our quarterly revenue reflects strong governance and compliance standards.
The board of directors approved the fiscal portfolio strategy.
Shareholder synergy drives our corporate vision.</p>
</body></html>
"""

ECOMMERCE_CONTENT = """<!DOCTYPE html>
<html><head><title>ShopNow</title>
<meta name="description" content="Shop the best deals online - free shipping on orders over $50">
</head><body>
<h1>Shop Now - Best Deals</h1>
<p>Buy premium products at unbeatable prices. Free shipping on all orders.
Add items to your cart and enjoy amazing deals.</p>
</body></html>
"""

MINIMAL_CONTENT = """<!DOCTYPE html>
<html><head><title>Simple</title></head>
<body><p>Hello world.</p></body></html>
"""

LONG_TAGLINE = """<!DOCTYPE html>
<html><head><title>Test</title></head>
<body>
<section class="hero">
  <h1>This is an extremely long heading that goes on and on and on and exceeds the maximum character limit for a tagline which should be under one hundred and twenty characters total</h1>
</section>
</body></html>
"""

META_TAGLINE_ONLY = """<!DOCTYPE html>
<html><head><title>Brand</title>
<meta name="description" content="Simple, powerful tools">
</head><body>
<h1>A Really Long Heading That Definitely Exceeds One Hundred And Twenty Characters And Therefore Should Not Be Used As A Tagline For The Brand</h1>
</body></html>
"""

COMPLEX_READING = """<!DOCTYPE html>
<html><head><title>Academic</title></head>
<body>
<p>Sophisticated methodological frameworks necessitate comprehensive analytical
investigation throughout organizational infrastructure implementations and
furthermore demonstrate transformational operational excellence across
departmental hierarchies establishing unprecedented institutional benchmarks.
Comprehensive institutional governance requirements substantially facilitate
transformational operational excellence across departmental hierarchies while
simultaneously incorporating multidisciplinary perspectives throughout
the comprehensive organizational restructuring initiative.</p>
</body></html>
"""
