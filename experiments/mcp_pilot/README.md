# RateCheck public-information MCP pilot (not deployed)

A minimal **read-only, local-only** Model Context Protocol server using the official Python SDK v2.

It provides three tools:
- `describe_ratecheck`: public description and the categories the current checker supports
- `prepare_ratecheck_check`: validates the basic information a user would enter before opening the existing web checker; **does not run an assessment**
- `get_official_business_rates_guidance`: official valuation, billing and challenge references

## Purpose and limitations

This is a narrow technical experiment to test whether users of AI clients want to navigate to RateCheck from an agent conversation. It is **not** an SEO replacement, not a ChatGPT search listing, not deployed remotely and **not connected** to the existing protected `/assess` API.

Do **not** bypass Cloudflare Turnstile, paywalls, approved user interaction, usage limits, data permissions, or existing report entitlement checks to produce answers via MCP. It exposes **no customer data, purchased reports or VOA bulk records**, does not submit checks, and makes **no network calls**.

## Local setup

Python 3.11+:

```bash
python -m pip install "mcp>=2,<3"
python experiments/mcp_pilot/server.py
```

The process speaks MCP over **stdio**, so normal stdout is reserved for protocol messages. Configure an MCP-compatible local client to launch the command above.

Use an MCP Inspector client if desired to test the tools. From the repository root:

```bash
python -m py_compile experiments/mcp_pilot/server.py
```

## Before any HTTP/remote production service

1. Confirm the commercial use case: do consumers actually want to initiate a business-rates assessment from an agent interface?
2. Design an explicit consent flow, privacy and retention policy, authenticated user sessions if needed, request rate limits, bot protection and access controls.
3. Introduce a dedicated public/agent API with its own abuse budget **only** after securing safe access to the underlying assessment logic. Existing CAPTCHA checks must remain enforced for the current web endpoint.
4. Verify UK property data coverage, scoring quality and all customer-facing rateable-value versus rates-payable terminology.
5. Deploy a **separate** authenticated MCP HTTP service, with TLS, proper OAuth/host security and monitoring, only if adoption warrants it.
6. Measure source → tool invocation → website visit → completed check → purchased report; MCP tool installation alone is not evidence of traffic or demand.

## Source guidance

- https://www.gov.uk/find-business-rates
- https://www.gov.uk/introduction-to-business-rates/how-your-rates-are-calculated
- https://www.gov.uk/government/collections/how-to-challenge-your-business-rates-valuation
- https://py.sdk.modelcontextprotocol.io/
