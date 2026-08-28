-- =============================================================================
-- Legacy Broking ERP Database & CDC Enablement Script (MS SQL Server)
-- =============================================================================
-- Creates the BROKER_ERP database, legacy client collateral and trade tables,
-- populates sample legacy records, and enables Debezium Change-Data-Capture.
-- =============================================================================

USE master;
GO

IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = 'BROKER_ERP')
BEGIN
    CREATE DATABASE BROKER_ERP;
END
GO

USE BROKER_ERP;
GO

-- Enable Change Data Capture on the database level
EXEC sys.sp_cdc_enable_db;
GO

-- -----------------------------------------------------------------------------
-- Legacy Table 1: TBL_TRADE_TRANSACTIONS (Raw Trade Execution ERP Table)
-- -----------------------------------------------------------------------------
IF OBJECT_ID('dbo.TBL_TRADE_TRANSACTIONS', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.TBL_TRADE_TRANSACTIONS (
        N_TRANS_ID          BIGINT IDENTITY(10001, 1) PRIMARY KEY,
        VC_BROKER_CODE      VARCHAR(32) NOT NULL,
        VC_CLIENT_CODE      VARCHAR(32) NOT NULL,
        N_CLIENT_CAT        INT NOT NULL,          -- 1=RETAIL, 2=HNI, 3=INSTITUTIONAL
        VC_SYMBOL           VARCHAR(32) NOT NULL,
        N_ORDER_QTY         INT NOT NULL,
        N_ORDER_PRICE_Paisa BIGINT NOT NULL,       -- Stored in Paisa (1 INR = 100 Paisa)
        N_UPFRONT_MARGIN_BP INT NOT NULL,          -- Stored in Basis Points (1% = 100 BP)
        N_PEAK_MARGIN_FLAG  TINYINT NOT NULL,      -- 1=Yes, 0=No
        VC_SEGMENT_TYPE     VARCHAR(16) NOT NULL,  -- FNO, CASH, COMMODITY
        DT_TRADE_TIME       VARCHAR(20) NOT NULL,  -- Legacy format: YYYYMMDDHHMMSS
        VC_CREATED_BY       VARCHAR(64) DEFAULT 'SYS_ERP'
    );
END
GO

-- -----------------------------------------------------------------------------
-- Legacy Table 2: TBL_CLIENT_COLLATERAL (Raw Client Collateral Allocation Table)
-- -----------------------------------------------------------------------------
IF OBJECT_ID('dbo.TBL_CLIENT_COLLATERAL', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.TBL_CLIENT_COLLATERAL (
        N_ALLOC_ID          BIGINT IDENTITY(50001, 1) PRIMARY KEY,
        VC_BROKER_CODE      VARCHAR(32) NOT NULL,
        VC_CLIENT_CODE      VARCHAR(32) NOT NULL,
        N_CASH_COLLATERAL   BIGINT NOT NULL,       -- Stored in Paisa
        N_NONCASH_COLLATVAL BIGINT NOT NULL,       -- Stored in Paisa
        N_HAIRCUT_PCT_BP    INT NOT NULL,          -- Basis Points
        DT_ALLOCATION_TIME  VARCHAR(20) NOT NULL   -- YYYYMMDDHHMMSS
    );
END
GO

-- -----------------------------------------------------------------------------
-- Enable CDC Capture on Both Tables for Debezium Log Mining
-- -----------------------------------------------------------------------------
EXEC sys.sp_cdc_enable_table
    @source_schema = N'dbo',
    @source_name   = N'TBL_TRADE_TRANSACTIONS',
    @role_name     = NULL,
    @supports_net_changes = 1;
GO

EXEC sys.sp_cdc_enable_table
    @source_schema = N'dbo',
    @source_name   = N'TBL_CLIENT_COLLATERAL',
    @role_name     = NULL,
    @supports_net_changes = 1;
GO

-- -----------------------------------------------------------------------------
-- Insert Initial Sample Legacy Data
-- -----------------------------------------------------------------------------
INSERT INTO dbo.TBL_TRADE_TRANSACTIONS (
    VC_BROKER_CODE, VC_CLIENT_CODE, N_CLIENT_CAT, VC_SYMBOL,
    N_ORDER_QTY, N_ORDER_PRICE_Paisa, N_UPFRONT_MARGIN_BP,
    N_PEAK_MARGIN_FLAG, VC_SEGMENT_TYPE, DT_TRADE_TIME
) VALUES 
('BROKER_001', 'CLI_RETAIL_101', 1, 'RELIANCE', 100, 250000, 2000, 1, 'CASH', '20260828233000'),
('BROKER_002', 'CLI_HNI_202',    2, 'INFY',     500, 180000, 2500, 1, 'FNO',  '20260828233100');
GO
