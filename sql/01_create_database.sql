-- Step 1 of 2: creates the ArisMemory database, the "ai" schema the AI works in, and the one procedure it may
-- use to create tables. Then run 02_create_ai_login.sql.
-- Run as an administrator, in SSMS or with: sqlcmd -S localhost -E -C -i sql\01_create_database.sql
-- Safe to run again: everything is created only if missing, and the procedure is updated.

IF DB_ID(N'ArisMemory') IS NULL
    CREATE DATABASE ArisMemory;
GO

USE ArisMemory;
GO

-- All tables the AI creates live in this schema, separate from anything else in the database.
IF SCHEMA_ID(N'ai') IS NULL
    EXEC (N'CREATE SCHEMA ai AUTHORIZATION dbo');
GO

-- The only way the AI can create a table. It runs with the owner's rights (EXECUTE AS OWNER), so the AI login
-- itself never needs CREATE TABLE or ALTER rights, which would also let it drop or truncate tables.
-- Every table gets the same frame: Id (primary key), the requested columns, CreatedAt, UpdatedAt and
-- DeletedAt (for soft deletes). Names and types are checked here as well as in the Python code.
CREATE OR ALTER PROCEDURE ai.CreateTable
    @TableName SYSNAME,
    @Columns   NVARCHAR(MAX)  -- JSON, e.g. [{"name": "Title", "type": "text", "required": true}]
WITH EXECUTE AS OWNER
AS
BEGIN
    SET NOCOUNT ON;

    IF @TableName IS NULL OR LEN(@TableName) > 64
       OR @TableName COLLATE Latin1_General_BIN NOT LIKE '[A-Za-z]%'
       OR @TableName COLLATE Latin1_General_BIN LIKE '%[^A-Za-z0-9_]%'
        THROW 50001, 'Invalid table name: use 1-64 letters, digits or _, starting with a letter.', 1;

    IF OBJECT_ID(N'ai.' + QUOTENAME(@TableName), N'U') IS NOT NULL
        THROW 50002, 'A table with that name already exists.', 1;

    IF (SELECT COUNT(*) FROM sys.tables WHERE schema_id = SCHEMA_ID(N'ai')) >= 50
        THROW 50003, 'Table limit reached: at most 50 tables.', 1;

    IF ISJSON(@Columns) = 0 OR LEFT(LTRIM(@Columns), 1) <> N'['
        THROW 50004, 'Columns must be a JSON array.', 1;

    DECLARE @cols TABLE (Ord INT NOT NULL, Name NVARCHAR(200) NULL, SqlType NVARCHAR(40) NULL, IsRequired BIT NOT NULL);

    INSERT INTO @cols (Ord, Name, SqlType, IsRequired)
    SELECT CAST(j.[key] AS INT),
           c.name,
           CASE c.type
               WHEN 'text'      THEN N'NVARCHAR(400)'
               WHEN 'long_text' THEN N'NVARCHAR(4000)'
               WHEN 'integer'   THEN N'INT'
               WHEN 'decimal'   THEN N'DECIMAL(18, 4)'
               WHEN 'boolean'   THEN N'BIT'
               WHEN 'date'      THEN N'DATE'
               WHEN 'datetime'  THEN N'DATETIME2(0)'
           END,
           ISNULL(c.required, 0)
    FROM OPENJSON(@Columns) AS j
    CROSS APPLY OPENJSON(j.[value]) WITH (name NVARCHAR(200) '$.name', type NVARCHAR(20) '$.type', required BIT '$.required') AS c;

    IF (SELECT COUNT(*) FROM @cols) NOT BETWEEN 1 AND 30
        THROW 50005, 'A table needs between 1 and 30 columns.', 1;

    IF EXISTS (SELECT 1 FROM @cols WHERE SqlType IS NULL)
        THROW 50006, 'Unknown column type. Use text, long_text, integer, decimal, boolean, date or datetime.', 1;

    IF EXISTS (SELECT 1 FROM @cols
               WHERE Name IS NULL OR LEN(Name) > 64
                  OR Name COLLATE Latin1_General_BIN NOT LIKE '[A-Za-z]%'
                  OR Name COLLATE Latin1_General_BIN LIKE '%[^A-Za-z0-9_]%')
        THROW 50007, 'Invalid column name: use 1-64 letters, digits or _, starting with a letter.', 1;

    IF EXISTS (SELECT 1 FROM @cols WHERE Name IN (N'Id', N'CreatedAt', N'UpdatedAt', N'DeletedAt'))
        THROW 50008, 'Id, CreatedAt, UpdatedAt and DeletedAt are added automatically.', 1;

    IF EXISTS (SELECT Name FROM @cols GROUP BY Name HAVING COUNT(*) > 1)
        THROW 50009, 'Each column name may only be used once.', 1;

    DECLARE @definitions NVARCHAR(MAX) = (
        SELECT N', ' + QUOTENAME(Name) + N' ' + SqlType + CASE WHEN IsRequired = 1 THEN N' NOT NULL' ELSE N' NULL' END
        FROM @cols
        ORDER BY Ord
        FOR XML PATH(''), TYPE
    ).value('.', 'NVARCHAR(MAX)');

    DECLARE @sql NVARCHAR(MAX) =
        N'CREATE TABLE ai.' + QUOTENAME(@TableName) + N' ('
        + N'Id INT IDENTITY(1, 1) NOT NULL PRIMARY KEY'
        + @definitions
        + N', CreatedAt DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME()'
        + N', UpdatedAt DATETIME2(0) NULL'
        + N', DeletedAt DATETIME2(0) NULL);';

    EXEC sys.sp_executesql @sql;
END;
GO
