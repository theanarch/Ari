-- Creates the ArisMemory database and its one table.
-- Run once as an administrator, in SSMS or with: sqlcmd -S localhost -E -i sql\01_create_database.sql

IF DB_ID(N'ArisMemory') IS NULL
    CREATE DATABASE ArisMemory;
GO

USE ArisMemory;
GO

IF OBJECT_ID(N'dbo.Memories', N'U') IS NULL
    CREATE TABLE dbo.Memories (
        Id        INT IDENTITY(1, 1) NOT NULL CONSTRAINT PK_Memories PRIMARY KEY,
        Topic     NVARCHAR(100)      NOT NULL,
        Content   NVARCHAR(4000)     NOT NULL,
        CreatedAt DATETIME2(0)       NOT NULL CONSTRAINT DF_Memories_CreatedAt DEFAULT SYSUTCDATETIME(),
        UpdatedAt DATETIME2(0)       NULL,
        -- Soft delete: the AI can only set this. The row itself stays, so a human can always restore it.
        DeletedAt DATETIME2(0)       NULL
    );
GO
