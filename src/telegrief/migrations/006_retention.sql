-- Undelivered posts now wait 90 days by default (delivered ones are kept for
-- good): move the folders still on the old 7-day default.
UPDATE folders SET max_age_d = 90 WHERE max_age_d = 7;
