# DeepSift Search privacy policy

Effective date: 4 November 2026. Provided by Aadstra Labs (contact@aadstra.com).

This policy covers **DeepSift Search**: the hosted Apify Actor at
`https://aadstra--deepsift-search.apify.actor` and the `deepsift-search` Claude Code plugin that connects to it.
The free self-hosted DeepSift in this repository runs entirely on your own computer and sends nothing to us.

## What is sent, and where

- **Your searches and page addresses.** When you search or ask to read a page, the request goes to the
  DeepSift Search Actor running on [Apify](https://apify.com). DeepSift sends the search to Google through
  Apify's proxy and downloads the result pages from their websites, so those sites see a normal page request
  from Apify's servers, not from you.
- **Your Apify token.** The plugin sends your Apify token to Apify with each request. Apify uses it to check who
  you are and to bill your Apify account. In Claude Code, the token is kept in your system's secure storage.
  We never see it.

## What is kept

- **A short cache.** While the server is running, searches and the pages read for them are kept in memory and
  temporary files for up to 24 hours, so the same search can be answered again quickly. They are deleted when
  the server stops.
- **Normal runs.** If you start a normal (batch) run in Apify Console, the results are saved in your own Apify
  account's dataset. You control and delete them there.
- **Apify's records.** Apify keeps run records, logs and billing records as described in
  [Apify's privacy policy](https://apify.com/privacy-policy). As the Actor's developer we can see run
  statistics, error logs and the number of charged events, but we do not keep a separate copy of your searches.

## What we don't do

We don't sell or share your data, show ads, build profiles, or use your searches to train AI models.

## Your choices

Stop using the Actor or remove the plugin at any time (`/plugin uninstall deepsift-search@deepsift`). To ask
about or delete anything linked to you, email contact@aadstra.com.

## Changes

If this policy changes, the new version will be published here with a new effective date.
