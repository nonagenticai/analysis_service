"""HTML fixtures for industry detection tests."""

SAAS_WEBSITE = """<!DOCTYPE html>
<html><head><title>CloudPlatform - SaaS Dashboard</title>
<meta name="description" content="Cloud-based SaaS platform for workflow automation and integration">
</head><body>
<h1>Automate Your Workflow</h1>
<h2>Cloud Platform Dashboard</h2>
<p>Our scalable software platform provides API integration and automation tools.
Deploy with confidence using our cloud infrastructure with 99.9% uptime SLA.</p>
</body></html>
"""

ECOMMERCE_WEBSITE = """<!DOCTYPE html>
<html><head><title>ShopNow - Online Store</title>
<meta name="description" content="Shop the best products online with free shipping">
</head><body>
<h1>Shop Our Latest Collection</h1>
<h2>Free Shipping on All Orders</h2>
<p>Browse our product catalog, add items to your cart, and checkout securely.
Enjoy great prices and fast shipping on every order. Sale items up to 50% discount.</p>
</body></html>
"""

MEDIA_WEBSITE = """<!DOCTYPE html>
<html><head><title>Daily News - Breaking Stories</title>
<meta name="description" content="Latest breaking news and articles from our journalists">
</head><body>
<h1>Breaking News Today</h1>
<h2>Latest Articles</h2>
<p>Our journalists and reporters publish the latest news stories and press coverage.
Read our editor picks and breaking news content.</p>
</body></html>
"""

HEALTHCARE_WEBSITE = """<!DOCTYPE html>
<html><head><title>MediCare - Patient Portal</title>
<meta name="description" content="Medical patient portal for clinical care and treatment">
</head><body>
<h1>Your Health, Our Priority</h1>
<h2>Patient Care Portal</h2>
<p>Access your medical records, schedule doctor appointments, and manage your health.
Our clinical team provides comprehensive diagnosis and treatment options.</p>
</body></html>
"""

FINANCE_WEBSITE = """<!DOCTYPE html>
<html><head><title>InvestWise - Portfolio Management</title>
<meta name="description" content="Investment portfolio management and wealth planning">
</head><body>
<h1>Smart Investing Starts Here</h1>
<h2>Portfolio Management</h2>
<p>Manage your investments, savings, and wealth portfolio. Our trading platform
offers comprehensive finance tools for mortgage, credit, and loan management.</p>
</body></html>
"""

EDUCATION_WEBSITE = """<!DOCTYPE html>
<html><head><title>EduLearn - Online Courses</title>
<meta name="description" content="Online education courses for students">
</head><body>
<h1>Learn Something New Today</h1>
<h2>Course Catalog</h2>
<p>Browse our curriculum of online courses. Students can earn certificates and degrees
through our education platform. Enroll in training programs today.</p>
</body></html>
"""

AGENCY_WEBSITE = """<!DOCTYPE html>
<html><head><title>Creative Co - Digital Agency</title>
<meta name="description" content="Creative digital agency for branding and design strategy">
</head><body>
<h1>Creative Design Agency</h1>
<h2>Our Portfolio</h2>
<p>We are a creative agency specializing in branding, design, and digital strategy.
View our case studies and client portfolio. Schedule a consultation today.</p>
</body></html>
"""

NONPROFIT_WEBSITE = """<!DOCTYPE html>
<html><head><title>GoodCause - Donate Today</title>
<meta name="description" content="Nonprofit charity foundation supporting community causes">
</head><body>
<h1>Make a Difference Today</h1>
<h2>Our Mission</h2>
<p>Support our nonprofit foundation by donating to community causes. Volunteer with us
to make an impact. Our charity fundraising supports those in need.</p>
</body></html>
"""

AMBIGUOUS_WEBSITE = """<!DOCTYPE html>
<html><head><title>Generic Corp</title></head>
<body><h1>Welcome</h1><p>We do things.</p></body></html>
"""

SCHEMA_SAAS_WEBSITE = """<!DOCTYPE html>
<html><head><title>AppCo</title>
<script type="application/ld+json">
{"@type": "SoftwareApplication", "name": "AppCo", "url": "https://appco.com"}
</script>
</head><body><h1>AppCo Platform</h1></body></html>
"""
