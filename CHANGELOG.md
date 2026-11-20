# Changelog

## 0.3.0 - 2026-11-20

- Report CO2 and PM2.5 sensors on fresh-air (ERV) units. The extra columns are
  requested for every unit but only answered by models that carry the sensors, so
  air conditioners create no additional entities.

## 0.2.2 - 2026-10-09

- Start responsive units even when another unit is offline. Reload the integration
  after the skipped unit returns online to discover it again.
- Keep automatic setup retries when no units respond and clean up failed transports.

## 0.2.1 - 2026-09-11

- Add a self-contained Gree protocol layer with local UDP control and Gree+ cloud fallback.
- Expose each air conditioner as a native Home Assistant climate entity.
- Add HACS metadata, project branding, installation guidance, and account-safety instructions.
- Render the project icon correctly inside HACS.
