"""Synthetic sample catalog used only when the user passes --mock.

Every id starts with `mock_` and every URL points at a nonexistent video, so a sample record can
never be mistaken for a real source.
"""

from __future__ import annotations

MOCK_NOTEBOOK_ID = "mock_notebook_0000"
MOCK_NOTEBOOK_TITLE = "Sample Catalog (mock data)"

# Sample titles, clustered by theme; ids and URLs are synthetic.
SAMPLE_VIDEOS: list[dict[str, str]] = [
    {
        "id": "mock_001",
        "title": "💻📕✈️ How I Have Time to Run a Business, Homeschool My Kids & Travel Regularly",
        "url": "https://www.youtube.com/watch?v=mock_001",
    },
    {
        "id": "mock_002",
        "title": "🤔 How You're Building Websites Could Be Completely Wrong",
        "url": "https://www.youtube.com/watch?v=mock_002",
    },
    {
        "id": "mock_003",
        "title": "😲 Quarterly Business Reviews: Get MORE Work From PAST & CURRENT Clients",
        "url": "https://www.youtube.com/watch?v=mock_003",
    },
    {
        "id": "mock_004",
        "title": "😲 This SEO tool has a CRM, Task Manager, Marketing Calendar, Client Portal & More",
        "url": "https://www.youtube.com/watch?v=mock_004",
    },
    {
        "id": "mock_005",
        "title": "🚀 The 1 Lead Generation Strategy That Helped My Business Takeoff",
        "url": "https://www.youtube.com/watch?v=mock_005",
    },
    {
        "id": "mock_006",
        "title": "🚨 Beware of This GBP Scam (Don’t Let Your Clients Fall for It)",
        "url": "https://www.youtube.com/watch?v=mock_006",
    },
    {
        "id": "mock_007",
        "title": "How to Price Website Projects ($3k to $10k Website Pricing Guide)",
        "url": "https://www.youtube.com/watch?v=mock_007",
    },
    {
        "id": "mock_008",
        "title": "Cold Email Outreach Scripts That Actually Book High-Ticket Web Design Clients",
        "url": "https://www.youtube.com/watch?v=mock_008",
    },
    {
        "id": "mock_009",
        "title": "Google Business Profile Optimization Blueprint: Rank #1 in Local 3-Pack",
        "url": "https://www.youtube.com/watch?v=mock_009",
    },
    {
        "id": "mock_010",
        "title": "Monthly SEO Retainers: How to Sell $1,500/mo Maintenance Packages",
        "url": "https://www.youtube.com/watch?v=mock_010",
    },
    {
        "id": "mock_011",
        "title": "Agency Client Onboarding Workflow: The Exact Process From Contract to Kickoff",
        "url": "https://www.youtube.com/watch?v=mock_011",
    },
    {
        "id": "mock_012",
        "title": "Local Service Ads (LSA) vs Google Ads: Which Delivers Better ROI for Local Businesses",
        "url": "https://www.youtube.com/watch?v=mock_012",
    },
]

# Generate the remainder of the 288 realistic entries matching the channel taxonomy
_THEME_TEMPLATES = [
    (
        "Lead Generation & Client Acquisition",
        [
            "Cold Calling Local Businesses: Word-for-Word Scripts That Work",
            "LinkedIn Lead Generation for Digital Marketing Agencies",
            "How to Get Web Design Clients Using Loom Video Audits",
            "Facebook Groups to $10k Months: Organic Client Acquisition",
            "The Direct Mail Strategy Bringing in 5-Figure Local Clients",
            "How to Partner with Accountants and Lawyers for Endless Referrals",
        ],
    ),
    (
        "Web Design & Agency Operations",
        [
            "WordPress vs Webflow: Which Platform Should Your Agency Standardize On",
            "How to Structure a 5-Page Local Business Website Wireframe",
            "Client Feedback Nightmare: How to Avoid Endless Revision Rounds",
            "Figma to WordPress: Speed Up Development Time by 50%",
            "Standard Operating Procedures (SOPs) Every Web Designer Needs",
            "White Label Web Design Partners: Pros, Cons, and Vendor Vetting",
        ],
    ),
    (
        "Client Retention, Onboarding & QBRs",
        [
            "How to Stop Client Churn: The 90-Day Retention Playbook",
            "Quarterly Business Reviews: Slide Deck Template & Walkthrough",
            "Automated Client Reporting Dashboards That Prove SEO Value",
            "How to Upsell Past Clients Without Feeling Salesy",
            "Handling Difficult Clients: How to Fire a Problem Customer Gracefully",
        ],
    ),
    (
        "Pricing & Proposal Strategy",
        [
            "Value-Based Pricing vs Hourly Rates: The Complete Agency Math",
            "Proposal Templates That Close: From First Discovery Call to Signed Contract",
            "How to Raise Your Rates on Existing Clients Without Losing Them",
            "Payment Terms for Freelancers: 50/50 vs Milestones vs Monthly Autopay",
            "How to Respond When a Client Says 'Your Price Is Too High'",
        ],
    ),
    (
        "SEO & Local Search",
        [
            "Local Citation Building: White Hat Strategies That Move the Needle",
            "On-Page SEO Checklist for Local Service Business Websites",
            "How to Audit a Google Business Profile in 15 Minutes",
            "Schema Markup for Local SEO: LocalBusiness JSON-LD Guide",
            "Geo-Grid Tracking Tools: How to Show Ranking Visibility Across a City",
        ],
    ),
    (
        "Solo Operator Systems & Productivity",
        [
            "The Tech Stack I Use to Run a 6-Figure Agency Solo",
            "Time Blocking for Entrepreneurs: How to Work 25 Hours a Week",
            "ClickUp vs Notion: The Best Project Management Tool for Freelancers",
            "Automating Invoicing, Contracts, and Payments in One Workflow",
            "Burnout Recovery: How to Systematize Your Business When Overwhelmed",
        ],
    ),
]

SAMPLE_SIZE = 288


def _fill_sample(videos: list[dict[str, str]]) -> None:
    """Extend the hand-written entries to SAMPLE_SIZE with titles cycled through the themes."""
    idx = len(videos) + 1
    while len(videos) < SAMPLE_SIZE:
        for _theme, templates in _THEME_TEMPLATES:
            if len(videos) >= SAMPLE_SIZE:
                break
            template = templates[len(videos) % len(templates)]
            variant = len(videos) // len(templates) + 1
            videos.append(
                {
                    "id": f"mock_{idx:03d}",
                    "title": f"{template} (Vol. {variant})",
                    "url": f"https://www.youtube.com/watch?v=mock_{idx:03d}",
                }
            )
            idx += 1


_fill_sample(SAMPLE_VIDEOS)


def get_mock_sources() -> list[dict[str, str]]:
    """Return every synthetic sample source."""
    return list(SAMPLE_VIDEOS)
