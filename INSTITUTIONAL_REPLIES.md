# Institutional inquiry replies

Standard responses for emails coming via `/institutional`.

The institutional page's job in year 1 is **market research and strategic
permission**, not closing deals. The first 3+ conversations are how you
learn what institutional buyers actually need before you commit to
features or pricing. Don't quote prices in email — get on a call.

## Standard first reply (first contact, no detail)

For: "Hi, saw xrpldashboard. Curious about institutional access."

> Thanks for reaching out — really appreciate it.
>
> I'm Charlie, the developer behind xrpldashboard. I'm currently in the
> first round of conversations with institutional users, which is the
> phase before we publish prices, so I'd rather hop on a 30-minute call
> than reply with a tier sheet.
>
> What I'd love to learn on that call:
>
> 1. What XRPL data your team is currently using and where the gaps are
> 2. The format you'd actually want to consume it in (API / dashboard /
>    flat-file dump / something else)
> 3. Whether what we're building lines up with that, or whether there's a
>    different shape that would be more useful
>
> If a call works, here's my [Calendly / scheduling link / "send me a few
> times that work and I'll match"]: __________
>
> If you'd rather start with a few specifics over email, happy to do that
> too — just let me know what's most useful.
>
> — Charlie

## Standard first reply (specific use case mentioned)

For: "Hi, we're a [fund / exchange / index team] looking for [specific
data]. What does access look like?"

> Thanks for the note — and the specificity is helpful.
>
> Short answer on access: I'm in the first round of partner conversations,
> which is the window before published pricing locks in. I'd rather
> understand what your team actually needs before quoting anything.
>
> On [the specific data they mentioned] — [one sentence on whether
> xrpldashboard already tracks it, partially tracks it, or would be a
> new addition; be honest if it's the latter].
>
> Could we hop on 30 minutes? I'm trying to learn from people in your
> seat before I commit to a feature roadmap, and concrete use cases like
> yours are exactly what I want to hear.
>
> [scheduling link or "send me a few times"]: __________
>
> — Charlie

## Standard reply (asking only for pricing, no context)

For: "Just send me your pricing tiers."

> Thanks for the interest — and totally fair to ask up front.
>
> I'm intentionally not publishing tiered pricing yet. The institutional
> tier is in a partner-conversation phase: I'm trying to learn what
> different teams actually need so the eventual pricing matches the
> value rather than being a guess.
>
> Practically, that means contracts I've signed in this phase have ranged
> from the low five figures to the mid five figures annually, depending
> on access scope. If your team's budget is in that ballpark and a
> 30-minute call to dig into use case sounds worthwhile, I'd love to set
> one up: [scheduling link]
>
> If your budget is well outside that range either direction, also worth
> knowing — there are lighter-weight ways I can help (data dumps, custom
> queries) that don't require a full institutional contract.
>
> — Charlie

## What NOT to send

- A list of features or a tier sheet without a call first
- A specific dollar number tied to a specific scope
- A promise of features that aren't built yet
- Anything that looks like a generic SaaS sales template — your edge is
  that you reply personally, not as "the team"

## After the call

Save call notes to `partner_conversations.md` (gitignored — this is your
private notes file, not a public doc). Track:

- Company / role
- What data they're already using and what it costs them
- What gaps they identified
- What would make them sign vs. not sign
- Their honest budget range
- Whether they'd be willing to be a reference customer if it works out

After 3+ of these, you have enough signal to draft real pricing tiers
and update `/institutional` accordingly.
