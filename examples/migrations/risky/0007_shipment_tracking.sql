-- Looks routine. Every statement here is a real outage on a busy table.
BEGIN;

ALTER TABLE shipment ADD COLUMN public_id uuid DEFAULT gen_random_uuid();

ALTER TABLE shipment
    ADD CONSTRAINT shipment_hub_fk FOREIGN KEY (hub_id) REFERENCES hub (id);

ALTER TABLE shipment ALTER COLUMN tracking_code SET NOT NULL;

CREATE UNIQUE INDEX shipment_tracking_code_key ON shipment (tracking_code);

UPDATE shipment SET status = 'pending' WHERE status = 'booked';

COMMIT;
