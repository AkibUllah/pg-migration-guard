-- The same change, one short lock at a time.
SET lock_timeout = '2s';

ALTER TABLE shipment ADD COLUMN public_id uuid;
ALTER TABLE shipment ALTER COLUMN public_id SET DEFAULT gen_random_uuid();
ALTER TABLE shipment ADD CONSTRAINT shipment_hub_fk
    FOREIGN KEY (hub_id) REFERENCES hub (id) NOT VALID;
ALTER TABLE shipment ADD CONSTRAINT shipment_tracking_code_nn
    CHECK (tracking_code IS NOT NULL) NOT VALID;
