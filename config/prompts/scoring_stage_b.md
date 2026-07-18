You are analyzing ONE website visitor's behavior to classify their position in a
marketing funnel. Base your judgment only on the evidence provided.

Funnel stages — choose exactly one for `funnel_stage`:
- TOFU — top of funnel: early awareness, mostly blog/home reading, low buying intent.
- MOFU — middle of funnel: actively evaluating; features, testimonials, FAQ, comparisons; moderate intent.
- BOFU — bottom of funnel: close to buying; pricing and checkout activity; high intent.
Allowed values: <<STAGES>>

Visitor feature summary and recent events (JSON):
<<FEATURES>>

Return a SINGLE JSON object with EXACTLY these keys:
- "funnel_stage": one of <<STAGES>>
- "intent_score": integer 0-100 (0 = no purchase intent, 100 = ready to buy now)
- "likely_objections": array of short strings — buying objections this visitor
  probably has given their behavior (empty array if none are evident)
- "persona_signals": object of inferred persona attributes you can support from
  the evidence (e.g. "role", "seniority", "company_size", "industry", "urgency");
  use an empty object if nothing can be inferred

Respond with ONLY the JSON object. No prose, no explanation, no markdown fences.
