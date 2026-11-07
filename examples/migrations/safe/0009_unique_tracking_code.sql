-- Needs no transaction wrapper: CONCURRENTLY cannot run inside one.
CREATE UNIQUE INDEX CONCURRENTLY shipment_tracking_code_key ON shipment (tracking_code);
