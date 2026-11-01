-- Fuel Consumption Analysis System: schema
-- Run:  mysql -u root -p < schema.sql
-- Demo users and sample data are added separately with:  python seed_demo.py

CREATE DATABASE IF NOT EXISTS fuel_consumption_db;
USE fuel_consumption_db;

-- One row per account. role = 'admin' sees and edits everything;
-- role = 'user' sees and edits only the vehicles they own.
CREATE TABLE IF NOT EXISTS users (
    user_id       INT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(50)  NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role          ENUM('admin', 'user') NOT NULL DEFAULT 'user',
    created_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Vehicle codes only need to be unique per owner, so two users can both own CAR001.
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id   INT AUTO_INCREMENT PRIMARY KEY,
    owner_id     INT         NOT NULL,
    vehicle_code VARCHAR(30) NOT NULL,
    vehicle_type VARCHAR(30) NOT NULL,
    fuel_type    VARCHAR(30) NOT NULL,
    CONSTRAINT fk_vehicle_owner FOREIGN KEY (owner_id) REFERENCES users(user_id),
    UNIQUE KEY uq_owner_vehicle_code (owner_id, vehicle_code)
);

-- No CHECK on litres/odometer order on purpose: the app rejects bad input,
-- but the invalid-reading report must still surface bad rows imported directly.
CREATE TABLE IF NOT EXISTS fuel_records (
    record_id      INT AUTO_INCREMENT PRIMARY KEY,
    vehicle_id     INT           NOT NULL,
    fill_date      DATE          NOT NULL,
    odometer_km    DECIMAL(10,2) NOT NULL,
    litres         DECIMAL(8,3)  NOT NULL,
    total_cost     DECIMAL(10,2) NOT NULL,
    full_tank_flag TINYINT(1)    NOT NULL DEFAULT 1,
    CONSTRAINT fk_fuel_vehicle FOREIGN KEY (vehicle_id) REFERENCES vehicles(vehicle_id),
    INDEX idx_vehicle_date (vehicle_id, fill_date, record_id)
);

-- Upgrading an old database: the simplest route is to drop fuel_consumption_db,
-- run this file again, then run seed_demo.py. Existing rows cannot be assigned
-- to an owner automatically.