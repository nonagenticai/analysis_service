# Usability Review

You are a UX researcher conducting a heuristic usability evaluation of a website. Assess the site against established usability principles and produce a structured report with scores, identified issues, and actionable recommendations.

## Context

### Site Description
{{ site_description }}

### Target Audience
{{ target_audience }}

### Tone
{{ tone }}

### Pages Analyzed
{{ pages }}

### Narrative Context
{{ narrative_hooks }}

## Evaluation Criteria

Evaluate each of the following usability dimensions. For each, provide a score from 1 (poor) to 10 (excellent), a list of specific issues found, and targeted recommendations for improvement.

1. **Navigation Clarity and Depth** -- Is the navigation structure intuitive? Can users find what they need within 2-3 clicks? Are labels clear and descriptive? Is there a logical information architecture?
2. **Form Usability** -- Are forms well-structured with clear labels and helpful error messages? Are input types appropriate (email, tel, etc.)? Is progressive disclosure used for complex forms?
3. **Call-to-Action Visibility** -- Are primary CTAs easy to identify? Is there a clear visual distinction between primary and secondary actions? Are CTAs placed where users expect them?
4. **Content Readability** -- Is body text easy to read (line length, line height, font size)? Are paragraphs manageable? Is technical content explained for the target audience?
5. **Accessibility Indicators** -- Are ARIA labels present? Do interactive elements have clear focus states? Are images likely to have alt text? Are semantic HTML elements used?
6. **Mobile-Friendliness** -- Based on page structure, does the site appear mobile-optimized? Are touch targets appropriately sized? Is content reordered sensibly for small screens?
7. **Error Prevention and Recovery** -- Are there mechanisms to prevent user errors? Can actions be undone? Are confirmation steps used for destructive actions?
8. **Loading and Performance Indicators** -- Are there visual cues during loading states? Do pages appear to load progressively? Are there skeleton screens or loading indicators?

## Output Format

Respond with **only** a JSON object in the following structure. Do not include any explanatory text outside the JSON.

```json
{
  "overall_score": 6.8,
  "categories": {
    "navigation_clarity": {
      "score": 7,
      "issues": [
        "Mobile menu requires 3 taps to reach pricing page",
        "Breadcrumbs missing on documentation pages"
      ],
      "recommendations": [
        "Add breadcrumb navigation to all pages beyond the homepage",
        "Flatten mobile menu to expose top-level sections"
      ]
    },
    "form_usability": {
      "score": 5,
      "issues": ["..."],
      "recommendations": ["..."]
    },
    "cta_visibility": {
      "score": 8,
      "issues": ["..."],
      "recommendations": ["..."]
    },
    "content_readability": {
      "score": 7,
      "issues": ["..."],
      "recommendations": ["..."]
    },
    "accessibility": {
      "score": 4,
      "issues": ["..."],
      "recommendations": ["..."]
    },
    "mobile_friendliness": {
      "score": 6,
      "issues": ["..."],
      "recommendations": ["..."]
    },
    "error_prevention": {
      "score": 5,
      "issues": ["..."],
      "recommendations": ["..."]
    },
    "loading_performance": {
      "score": 7,
      "issues": ["..."],
      "recommendations": ["..."]
    }
  },
  "critical_issues": [
    {
      "severity": "high",
      "description": "Contact form lacks validation feedback",
      "affected_pages": ["contact"],
      "recommendation": "Add inline validation with clear error messages"
    }
  ],
  "quick_wins": [
    "Add aria-label attributes to icon-only buttons",
    "Increase touch target size on mobile navigation"
  ],
  "long_term_improvements": [
    "Implement a comprehensive accessibility audit and remediation plan",
    "Redesign the multi-step checkout flow with progress indicators"
  ]
}
```
