"""Example Notebook 288-video benchmark dataset for testing and dry-run."""

from __future__ import annotations

PORTABLE_ENTREPRENEUR_NOTEBOOK_ID = "00000000-0000-4000-8000-0000000000aa"
PORTABLE_ENTREPRENEUR_NOTEBOOK_TITLE = "Example Notebook"

# Realistic catalog of video titles representing the 288 YouTube sources
PORTABLE_ENTREPRENEUR_VIDEOS: list[dict[str, str]] = [
    {
        "id": "yt_001",
        "title": "💻📕✈️ How I Have Time to Run a Business, Homeschool My Kids & Travel Regularly",
        "url": "https://www.youtube.com/watch?v=mock001",
        "topic": "Solo Operator Systems & Productivity",
    },
    {
        "id": "yt_002",
        "title": "🤔 How You're Building Websites Could Be Completely Wrong",
        "url": "https://www.youtube.com/watch?v=mock002",
        "topic": "Web Design & Agency Operations",
    },
    {
        "id": "yt_003",
        "title": "😲 Quarterly Business Reviews: Get MORE Work From PAST & CURRENT Clients",
        "url": "https://www.youtube.com/watch?v=mock003",
        "topic": "Client Retention, Onboarding & QBRs",
    },
    {
        "id": "yt_004",
        "title": "😲 This SEO tool has a CRM, Task Manager, Marketing Calendar, Client Portal & More",
        "url": "https://www.youtube.com/watch?v=mock004",
        "topic": "Solo Operator Systems & Productivity",
    },
    {
        "id": "yt_005",
        "title": "🚀 The 1 Lead Generation Strategy That Helped My Business Takeoff",
        "url": "https://www.youtube.com/watch?v=mock005",
        "topic": "Lead Generation & Client Acquisition",
    },
    {
        "id": "yt_006",
        "title": "🚨 Beware of This GBP Scam (Don’t Let Your Clients Fall for It)",
        "url": "https://www.youtube.com/watch?v=mock006",
        "topic": "SEO & Local Search",
    },
    {
        "id": "yt_007",
        "title": "How to Price Website Projects ($3k to $10k Website Pricing Guide)",
        "url": "https://www.youtube.com/watch?v=mock007",
        "topic": "Pricing & Proposal Strategy",
    },
    {
        "id": "yt_008",
        "title": "Cold Email Outreach Scripts That Actually Book High-Ticket Web Design Clients",
        "url": "https://www.youtube.com/watch?v=mock008",
        "topic": "Lead Generation & Client Acquisition",
    },
    {
        "id": "yt_009",
        "title": "Google Business Profile Optimization Blueprint: Rank #1 in Local 3-Pack",
        "url": "https://www.youtube.com/watch?v=mock009",
        "topic": "SEO & Local Search",
    },
    {
        "id": "yt_010",
        "title": "Monthly SEO Retainers: How to Sell $1,500/mo Maintenance Packages",
        "url": "https://www.youtube.com/watch?v=mock010",
        "topic": "Pricing & Proposal Strategy",
    },
    {
        "id": "yt_011",
        "title": "Agency Client Onboarding Workflow: The Exact Process From Contract to Kickoff",
        "url": "https://www.youtube.com/watch?v=mock011",
        "topic": "Client Retention, Onboarding & QBRs",
    },
    {
        "id": "yt_012",
        "title": "Local Service Ads (LSA) vs Google Ads: Which Delivers Better ROI for Local Businesses",
        "url": "https://www.youtube.com/watch?v=mock012",
        "topic": "SEO & Local Search",
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

_idx = len(PORTABLE_ENTREPRENEUR_VIDEOS) + 1
while len(PORTABLE_ENTREPRENEUR_VIDEOS) < 288:
    for theme, templates in _THEME_TEMPLATES:
        if len(PORTABLE_ENTREPRENEUR_VIDEOS) >= 288:
            break
        tmpl = templates[len(PORTABLE_ENTREPRENEUR_VIDEOS) % len(templates)]
        variant = len(PORTABLE_ENTREPRENEUR_VIDEOS) // len(templates) + 1
        title = f"{tmpl} (Vol. {variant})"
        PORTABLE_ENTREPRENEUR_VIDEOS.append(
            {
                "id": f"yt_{_idx:03d}",
                "title": title,
                "url": f"https://www.youtube.com/watch?v=mock_{_idx:03d}",
                "topic": theme,
            }
        )
        _idx += 1


def get_mock_sources() -> list[dict[str, str]]:
    """Return all 288 simulated YouTube video sources."""
    return list(PORTABLE_ENTREPRENEUR_VIDEOS)
