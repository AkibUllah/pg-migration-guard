-- Runs after a commit. VALIDATE allows reads and writes while it scans.
SET lock_timeout = '2s';

ALTER TABLE shipment VALIDATE CONSTRAINT shipment_hub_fk;
ALTER TABLE shipment VALIDATE CONSTRAINT shipment_tracking_code_nn;
ALTER TABLE shipment ALTER COLUMN tracking_code SET NOT NULL;
ALTER TABLE shipment DROP CONSTRAINT shipment_tracking_code_nn;
