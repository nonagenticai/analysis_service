You are a product strategist. Generate 8-15 feature improvement proposals for this website.

Site: {{ site_description }}
Audience: {{ target_audience }}

IMPORTANT: Base proposals ONLY on the data provided above. Do NOT fabricate technology stack details, vulnerabilities, or server configuration findings.

Your response MUST be ONLY a valid JSON object with this exact structure:
{
  "total_proposals": <number>,
  "proposals": [
    {
      "id": 1,
      "title": "...",
      "category": "quick_win|strategic_improvement|long_term_investment|nice_to_have",
      "impact": "high|medium|low",
      "complexity": "high|medium|low",
      "description": "...",
      "rationale": "..."
    }
  ]
}

No markdown fencing, no explanatory text. Only the JSON object.
{% if prior_findings %}

Prior findings:
{{ prior_findings }}
{% endif %}
{% if focus %}

Focus: {{ focus }}
{% endif %}
