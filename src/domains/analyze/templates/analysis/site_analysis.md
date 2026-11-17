{% if context_block %}
## User Context

{% if context_block.description %}**Description:** {{ context_block.description }}
{% endif %}
{% if context_block.site_type %}**Site type:** {{ context_block.site_type }}
{% endif %}
{% if context_block.target_audience %}**Target audience:** {{ context_block.target_audience }}
{% endif %}

{% endif %}
Analyze this website and provide a JSON response with the following structure:
{
  "description": "1-2 sentence site description",
  "target_audience": ["audience1", "audience2"],
  "use_cases": ["use case 1", "use case 2"],
  "selling_points": ["point 1", "point 2"],
  "narrative_hooks": ["hook 1", "hook 2"],
  "tone": "professional/casual/technical/etc",
  "visual_identity": {"primary_color": "#hex", "style": "modern/classic/etc"}
}

## Pages Discovered

{% for page in pages %}
- **{{ page.title | default(page.url) }}** ({{ page.page_type | default('unknown') }}): {{ page.word_count | default(0) }} words, {{ (page.interactive_elements | default([])) | length }} interactive elements, headings: {{ (page.headings | default([]))[:20] | join(', ') }}
{% if page.text is defined and page.text %}

Page content (truncated):
{{ page.text[:6000] }}
{% endif %}
{% endfor %}
