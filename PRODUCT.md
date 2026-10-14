# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
A single self-hoster (the owner), opening the admin occasionally, often from a phone, to
configure apps and check on builds. Knows Morphe, patches and APK terms; no other
audience is assumed.

## Product Purpose
Automates Morphe-patched Android apps: pulls patches from user-added GitHub sources,
downloads the matching APK (APKMirror via a ShardX browser, Morphe API, APKPure fallback),
patches it on a schedule, and publishes the result. Success is a current patched APK for
every configured app without manual work.

## Positioning
Self-hosted and scheduled, built around Morphe patch sources, with write-only secrets and
no hosted service.

## Operating Context
Runs in Docker or locally under uvicorn on the owner's machine. Builds run in the
background on a cron schedule or on demand; progress and failures show in the UI and in
notifications (Apprise).

## Capabilities and Constraints
- Tabs: Pipeline (configured apps and their build state), Discovery (apps available from
  sources), Settings (sources, schedule, notifications, GitHub token).
- The first thing the owner must see: which apps are configured, whether each is
  patched, and whether an update is available.
- No third-party or CDN assets; everything is served locally.
- Secrets (GitHub token, Apprise URLs) are write-only and never shown back.

## Brand Commitments
Name: Morphe Builder. No logo, voice or other binding brand commitments.

## Evidence on Hand
No customers, testimonials or benchmarks; none should be invented.

## Product Principles
- Status first: configured, patched and update state is readable at a glance.
- Quiet by default, loud on failure; never hide a failed build.
- Secrets never round-trip to the browser.
- Works the same on a phone as on a desktop.

## Accessibility & Inclusion
WCAG AA in light and dark, 44px touch targets, visible keyboard focus, reduced-motion
respected, reflow below 768px.
