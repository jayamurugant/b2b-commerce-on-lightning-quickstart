# Login History Report + Weekly Bar Chart Dashboard

This guide shows how to build a Salesforce **report** and **dashboard** that visualizes **user login history** as a **weekly bar chart**:

- **X axis**: Week (from the login Date/Time field)
- **Y axis**: Record Count (number of logins)
- **Grouped by**: User (so you can see counts per user, per week)

## Prerequisites

- You need permissions that allow you to access **Login History** reporting (typically admins).
- Note that Salesforce login history data retention can be limited (often ~6 months, org-dependent).

## Create the report (matrix, grouped by week and user)

1. Go to **Reports** → **New Report**.
2. Search for and select the report type **Login History** (usually under **Administrative Reports**) → **Start Report**.
3. Change the report format to **Matrix**.
4. **Group Rows by week**
   - In the outline, find the Login Date/Time field (commonly **Login Time**).
   - Drag it into **Row Groups**.
   - Click the group menu and set the date grouping to **Calendar Week**.
5. **Group Columns by user**
   - Find the user field (commonly **User** or **User: Full Name**).
   - Drag it into **Column Groups**.
6. Ensure the summary value is **Record Count**
   - The matrix should show counts automatically. If not, add the built-in **RowCount** / **Record Count** summary.
7. Add filters (recommended)
   - **Login Time**: for example **LAST 12 WEEKS** (or your preferred range).
   - **User**: optionally filter to a specific user (or keep it open for all users).
8. Add a chart to the report
   - Click **Add Chart**.
   - Choose **Bar** → **Stacked Bar** (recommended) or **Grouped Bar**.
   - **X-Axis**: Calendar Week (row grouping).
   - **Y-Axis**: Record Count.
   - **Stack/Group by**: User (column grouping).
9. Save the report
   - **Report Name**: `Login History by User by Week`
   - **Report Folder**: pick a shared folder your dashboard viewers can access.

## Create the dashboard (bar chart component)

1. Go to **Dashboards** → **New Dashboard**.
2. Name it: `Weekly Login History`.
3. Click **+ Component** and select the report you saved: `Login History by User by Week`.
4. Choose a **Bar Chart** display.
5. Configure the component to match the report chart (if prompted)
   - **X axis**: Week
   - **Y axis**: Record Count
   - **Grouped/Stacked by**: User
6. Save the dashboard.

## Optional: let viewers choose a user (dashboard filter)

If you want “grouped by particular user” to be interactive:

1. In the dashboard, add a **Dashboard Filter**.
2. Field: the **User** field from the report.
3. Let viewers select one or more users; the bar chart will update to show only those users.

## Quick validation checklist

- You see **one bar per week** on the x-axis.
- The y-axis shows **record count** (number of login events).
- Bars are **segmented (stacked)** or **split (grouped)** by **User**.

