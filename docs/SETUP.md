# Google Calendar & Tasks Setup Guide

Connect FiestaBoard to your Google account to show today's agenda, what's next, and the tasks due today.

## Overview

**What it does:**

- Shows today's events from one or more of your Google calendars, with start times
- Shows whether you're in a meeting right now, and until when
- Shows the next event to start, up to a week ahead, and how soon
- Shows the Google Tasks due today, from all your task lists

**What it can see:** FiestaBoard asks Google for two read-only permissions: to see your calendars and events (`calendar.readonly`) and to see your tasks (`tasks.readonly`). It can't add, change, or delete anything, and it can't see your email, contacts, or files.

**Prerequisites:**

- A Google account (a personal Gmail account or a Google Workspace account)
- About 10 minutes, once, to create a free OAuth client in your own [Google Cloud](https://console.cloud.google.com/) project. Google requires every app that reads calendars to have one, so each person brings their own; FiestaBoard walks you through it. There's no charge for this.
- FiestaBoard 9.8.0 or later (the plugin's settings have an **Account connection** section).
- Open FiestaBoard by its local address, such as `http://192.168.1.50:4420`, when you sign in.

## Quick Setup

### 1. Enable the Plugin

In the FiestaBoard web UI:

1. Go to **Integrations**
2. Find **Google Calendar & Tasks** and toggle it **On**
3. Click **Configure**. The **Account connection** section at the top lists the steps below and shows the redirect URI you'll need, with a copy button.

### 2. Create a Google Cloud project and turn on the APIs

1. Go to the [Google Cloud console](https://console.cloud.google.com/) and sign in with your Google account. Accept the terms if this is your first visit.
2. In the project picker at the top, choose **New project**, name it something like `FiestaBoard`, and press **Create**. Make sure the new project is selected.
3. Open **APIs & Services** > **Library**. Search for **Google Calendar API**, open it and press **Enable**. Go back to the Library, search for **Google Tasks API**, and press **Enable** on that too. (If you'll turn off **Show Tasks**, you can skip the Tasks API.)

### 3. Set up the consent screen

1. Open **Google Auth platform** (search for it in the console's search bar). If it asks you to **Get started**, fill in:
   - **App name:** anything, such as `FiestaBoard`
   - **User support email:** your email address
   - **Audience:** **External** (with a Google Workspace account you can choose **Internal** instead, and then skip step 3 below)
   - **Contact information:** your email address
   - Agree to the Google API Services User Data Policy and press **Create**.
2. Optional but tidy: open **Data Access**, press **Add or remove scopes**, and add `.../auth/calendar.readonly` and `.../auth/tasks.readonly`. Press **Update**, then **Save**.
3. **Important:** open **Audience** and, under **Publishing status**, press **Publish app** and confirm, so the status reads **In production**. While an External app is in **Testing**, Google lets its sign-ins last only **7 days**: after a week the board would stop showing your calendar until you sign in again. Publishing your own app doesn't make it public and doesn't need Google's review for your own use; it only changes how long the sign-in lasts.

### 4. Create the OAuth client

1. In **Google Auth platform**, open **Clients** and press **Create client**.
2. **Application type:** **Web application**
3. **Name:** anything, such as `FiestaBoard`
4. Leave **Authorized JavaScript origins** empty.
5. Under **Authorized redirect URIs**, press **Add URI** and paste the redirect URI from the plugin's **Account connection** section (use its copy button). It looks like `https://fiestaboard.app/auth/oauth/redirect`. Google compares this address exactly, so copy the one your board shows rather than typing it.
6. Press **Create**. Google shows the **Client ID** (ending in `.apps.googleusercontent.com`) and the **Client secret** (starting with `GOCSPX-`). Copy both now, or download the JSON: Google may not show the full secret again. If you lose it, add a new secret on the client's page.

### 5. Sign in

1. Back in the plugin's **Account connection** section, paste the **Client ID** and **Client Secret**
2. Press **Sign in with Google**. It saves both and takes you to Google.
3. Choose your Google account. Because the app is yours and unverified, Google warns **Google hasn't verified this app**. Press **Advanced**, then **Go to FiestaBoard (unsafe)**. This is expected for an app you made for yourself.
4. Google lists the two permissions. Tick both boxes if they're shown, then press **Continue**
5. On the way back you pass through `fiestaboard.app`. The first time, it shows your board's address and asks you to confirm it: check that it's the address you use for FiestaBoard and press **Continue to my board**
6. You land back on **Integrations** with Google connected

Optional settings:

- **Calendars:** calendar IDs to show, separated by commas (default `primary`, your main calendar). To find another calendar's ID, open [Google Calendar](https://calendar.google.com/), go to **Settings**, pick the calendar under **Settings for my calendars**, and copy **Calendar ID** under **Integrate calendar**. Shared calendars work too.
- **Show Declined Events:** include events you said No to (off by default)
- **Show All-Day Events:** include holidays, birthdays and other all-day events (on by default)
- **Show Tasks:** read Google Tasks (on by default)
- **Include Overdue Tasks:** also list open tasks whose due date has passed (off by default)
- **Time Format:** `9:30AM` or `09:30`
- **Timezone:** which day counts as today; leave empty to use FiestaBoard's timezone
- **Refresh Interval:** how often to check Google, 60 to 3600 seconds (default 300)

More about what you see while connecting: [Connecting Accounts](https://fiestaboard.app/docs/features/connecting-accounts).

### 6. Create a Board Template

Create a page from the plugin's demo, or add variables to your own page. Center-align the lines:

```jinja
{{google_calendar.busy_tile}} {{google_calendar.headline}}
{{google_calendar.events.0.line}}
{{google_calendar.events.1.line}}
{{google_calendar.events.2.line}}
{{google_calendar.events.3.line}}
{{google_calendar.tasks_line}}
```

On a Note, try:

```jinja
NEXT {{google_calendar.next_in}}
{{google_calendar.next_title}}
{{google_calendar.next_time}} {{google_calendar.next_day}}
```

### 7. View on Your Board

The board shows your agenda on the next refresh (every 5 minutes by default). A new or moved event appears after the next refresh.

**Limits:** Google limits an unverified app to 100 different Google accounts, which is plenty for a household. Each FiestaBoard plugin instance connects one Google account; add a second instance for a second person.

## Template Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `{{google_calendar.date_label}}` | Today's date | `FRI OCT 2` |
| `{{google_calendar.headline}}` | How many events today | `4 EVENTS TODAY` |
| `{{google_calendar.event_count}}` | Events today | `4` |
| `{{google_calendar.events_left}}` | Events today that haven't ended | `2` |
| `{{google_calendar.all_day_count}}` | All-day events today | `1` |
| `{{google_calendar.events.N.title}}`, `.time`, `.end_time`, `.line`, `.location` | Today's event N (0-9) | `9AM STANDUP` |
| `{{google_calendar.is_busy}}` | In a meeting now | `true` |
| `{{google_calendar.busy_tile}}` | Red tile when busy, green when free | `{66}` |
| `{{google_calendar.now_title}}` | The event happening now | `DESIGN REVIEW` |
| `{{google_calendar.now_until}}` | When it ends | `UNTIL 12:30PM` |
| `{{google_calendar.next_title}}` | Next event, within a week | `DESIGN REVIEW` |
| `{{google_calendar.next_time}}` | Its start time, or `ALL DAY` | `11:30AM` |
| `{{google_calendar.next_day}}` | `TODAY`, `TOMORROW` or a date | `TODAY` |
| `{{google_calendar.next_in}}` | How soon it starts | `IN 25M` |
| `{{google_calendar.next_location}}` | Its location | `ROOM 4B` |
| `{{google_calendar.next_line}}` | Start time and title | `11:30AM DESIGN REVIEW` |
| `{{google_calendar.tasks_due_count}}` | Tasks due today | `2` |
| `{{google_calendar.tasks_overdue_count}}` | Overdue tasks (with Include Overdue Tasks) | `1` |
| `{{google_calendar.tasks_line}}` | Tasks due as a sentence | `2 TASKS DUE TODAY` |
| `{{google_calendar.next_task}}` | First task due | `WATER THE PLANTS` |
| `{{google_calendar.tasks.N.title}}`, `.list`, `.overdue` | Task N (0-9) | `WATER THE PLANTS` |

## Configuration Reference

| Setting | Required | Default | Description |
|---------|----------|---------|-------------|
| Client ID | To sign in | | From your OAuth client in Google Auth platform > Clients. Entered in **Account connection** |
| Client Secret | To sign in | | From the same OAuth client. Entered in **Account connection**; stored on your board only |
| Calendars | No | `primary` | Calendar IDs, separated by commas |
| Show Declined Events | No | Off | Include events you declined |
| Show All-Day Events | No | On | Include all-day events |
| Show Tasks | No | On | Read Google Tasks |
| Include Overdue Tasks | No | Off | Also list tasks past their due date |
| Time Format | No | `12h` | `12h` or `24h` |
| Timezone | No | FiestaBoard's | Which day counts as today |
| Refresh Interval (seconds) | No | 300 | How often to check Google (60-3600) |

The connection itself is made with the **Sign in with Google** button, not a setting. The plugin asks Google for two read-only permissions: `https://www.googleapis.com/auth/calendar.readonly` and `https://www.googleapis.com/auth/tasks.readonly`.

**Environment variables:** none. The Client ID and Client Secret are entered in the plugin's settings.

## Troubleshooting

**"Not signed in to Google"**

- Press **Sign in with Google** (or **Reconnect**) in the plugin's **Account connection** section. If the button is greyed out, paste your Client ID and Client Secret first.
- **Reconnect needed** after about a week means the consent screen is still in **Testing**. Open **Google Auth platform** > **Audience**, press **Publish app** so it reads **In production**, then press **Reconnect**.
- It also happens if you removed FiestaBoard's access in your [Google Account permissions](https://myaccount.google.com/permissions), or deleted the OAuth client or its secret.

**Google shows "Error 400: redirect_uri_mismatch"**

- The client's **Authorized redirect URIs** must contain exactly the redirect URI shown in the plugin's **Account connection** section, with no trailing slash or spaces. Copy it again, add it to the OAuth client, press **Save**, wait a minute, and sign in again.
- Check the client's type is **Web application**. Desktop and other types don't accept this redirect URI.

**Google shows "Error 401: invalid_client"** or **"The provider rejected the sign-in"**

- The Client ID or Client Secret doesn't match your OAuth client. Copy both again from **Google Auth platform** > **Clients**. If you no longer have the secret, add a new one on the client's page.

**Google shows "Error 403: access_denied" saying the app is being tested**

- The consent screen is in **Testing** and your account isn't a test user. Publish the app (step 3), or add your account under **Audience** > **Test users**.

**"Google Calendar API is not enabled in your Google Cloud project"** or **"Google Tasks API is not enabled"**

- In the same Google Cloud project, open **APIs & Services** > **Library**, find the API and press **Enable**. Wait a few minutes. If you don't want tasks, turn off **Show Tasks** instead.

**"Google did not give FiestaBoard permission to read your calendars"** (or **tasks**)

- A permission box was left unticked on Google's consent screen. Press **Reconnect** and tick both boxes.

**"Google rejected the sign-in (401)"**

- Google stopped accepting the saved sign-in. Press **Reconnect**.

**"Calendar not found: ..."**

- That calendar ID is mistyped, or the calendar isn't shared with the account you signed in with. Check **Calendars** in the plugin's settings against **Calendar ID** in Google Calendar's settings.

**"Google rate limit reached"**

- The plugin waits as long as Google asks and keeps showing today's last data meanwhile. If it happens often, raise the refresh interval.

**An event is missing**

- Declined events are hidden unless **Show Declined Events** is on, and all-day events unless **Show All-Day Events** is on. Google's "working location" entries are always left out.
- The calendar it's on must be listed in **Calendars**. `primary` is only your main calendar.

**Times or "today" are off by some hours**

- Set **Timezone** in the plugin's settings, or check FiestaBoard's own timezone under **Settings**.

**"This FiestaBoard version cannot sign in to Google"**

- Update FiestaBoard to 9.8.0 or later.

**Plugin shows "Not Available"**

- The error next to the plugin on the Integrations page says what went wrong. For more detail, check the logs: `docker compose logs -f`
