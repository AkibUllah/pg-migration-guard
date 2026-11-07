-- The schema of a fictional parcel courier, used by the example migrations.
CREATE TABLE hub (
    id bigint PRIMARY KEY,
    city text NOT NULL
);
INSERT INTO hub VALUES (1, 'Dhaka'), (2, 'Chattogram'), (3, 'Sylhet');

CREATE TABLE shipment (
    id bigint PRIMARY KEY,
    hub_id bigint,
    tracking_code text,
    cod_amount_bdt numeric(12, 2),
    status text NOT NULL DEFAULT 'booked'
);
INSERT INTO shipment
SELECT g, g % 3 + 1, 'SC' || lpad(g::text, 8, '0'), (g % 50) * 100, 'booked'
FROM generate_series(1, 5000) g;
