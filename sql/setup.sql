-- Sets up the database for the ari-memory MCP server. Run it once as an administrator; it is safe to run
-- again later (it then updates the procedure and resets the login's password).
--
-- 1. Choose your own values in the four :setvar lines below.
-- 2. Run the script in SQLCMD mode:
--      SSMS:          Query menu > SQLCMD Mode, then Execute
--      Command line:  sqlcmd -S localhost -E -C -i sql\setup.sql
--    (Without SQLCMD mode the script stops at the first line and changes nothing.)
-- 3. Use the same database, login and password in the server's connection string.
--
-- Names may only contain letters, digits and _, and must start with a letter.
-- The password needs at least 8 characters with three of: upper case, lower case, digits and symbols.
-- It may not contain the login name, ' or ".

:setvar DatabaseName  "CHANGE_ME_database"
:setvar LoginName     "CHANGE_ME_login"
:setvar LoginPassword "CHANGE_ME_password"
:setvar SchemaName    "ai"
GO

-- Stop before changing anything if the script is not running in SQLCMD mode or a value is still a placeholder.
-- SET NOEXEC ON makes SQL Server skip every following batch; the last batch turns it off again.
IF N'$(DatabaseName)' = N'$' + N'(DatabaseName)'
BEGIN
    RAISERROR(N'Turn on SQLCMD mode (SSMS: Query > SQLCMD Mode) and run the script again.', 16, 1);
    SET NOEXEC ON;
END
ELSE IF N'$(DatabaseName)' LIKE N'CHANGE[_]ME%' OR N'$(LoginName)' LIKE N'CHANGE[_]ME%'
     OR N'$(LoginPassword)' LIKE N'CHANGE[_]ME%'
BEGIN
    RAISERROR(N'Choose your own DatabaseName, LoginName and LoginPassword at the top of setup.sql first.', 16, 1);
    SET NOEXEC ON;
END
ELSE IF EXISTS (
    SELECT 1 FROM (VALUES (N'$(DatabaseName)'), (N'$(LoginName)'), (N'$(SchemaName)')) AS v(Name)
    WHERE LEN(Name) NOT BETWEEN 1 AND 64
       OR Name COLLATE Latin1_General_BIN NOT LIKE '[A-Za-z]%'
       OR Name COLLATE Latin1_General_BIN LIKE '%[^A-Za-z0-9_]%')
BEGIN
    RAISERROR(N'DatabaseName, LoginName and SchemaName may only contain letters, digits and _, starting with a letter.', 16, 1);
    SET NOEXEC ON;
END
GO

-- The database ------------------------------------------------------------------------------------------------

IF DB_ID(N'$(DatabaseName)') IS NULL
    CREATE DATABASE [$(DatabaseName)];
GO

USE [$(DatabaseName)];
GO

-- All tables the AI creates live in this schema, separate from anything else in the database.
IF SCHEMA_ID(N'$(SchemaName)') IS NULL
    EXEC (N'CREATE SCHEMA [$(SchemaName)] AUTHORIZATION dbo');
GO

-- Procedures remember these two settings from when they were created. sqlcmd turns QUOTED_IDENTIFIER off by
-- default, and the procedure needs it on.
SET ANSI_NULLS ON;
SET QUOTED_IDENTIFIER ON;
GO

-- The only way the AI can create a table. It runs with the owner's rights (EXECUTE AS OWNER), so the AI login
-- itself never needs CREATE TABLE or ALTER rights, which would also let it drop or truncate tables.
-- Every table gets the same frame: Id (primary key), the requested columns, CreatedAt, UpdatedAt and
-- DeletedAt (for soft deletes). Names and types are checked here as well as in the Python code.
CREATE OR ALTER PROCEDURE [$(SchemaName)].CreateTable
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

    IF OBJECT_ID(N'$(SchemaName).' + QUOTENAME(@TableName), N'U') IS NOT NULL
        THROW 50002, 'A table with that name already exists.', 1;

    IF (SELECT COUNT(*) FROM sys.tables WHERE schema_id = SCHEMA_ID(N'$(SchemaName)')) >= 50
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
        N'CREATE TABLE [$(SchemaName)].' + QUOTENAME(@TableName) + N' ('
        + N'Id INT IDENTITY(1, 1) NOT NULL PRIMARY KEY'
        + @definitions
        + N', CreatedAt DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME()'
        + N', UpdatedAt DATETIME2(0) NULL'
        + N', DeletedAt DATETIME2(0) NULL);';

    EXEC sys.sp_executesql @sql;
END;
GO

-- The AI login -------------------------------------------------------------------------------------------------
-- What it can do:   read, insert and update rows in the schema's tables, and create new tables there
--                   through CreateTable.
-- What it cannot:   DELETE, DROP, TRUNCATE or ALTER anything, create tables any other way,
--                   or touch anything outside the schema.
-- SQL Server enforces this itself, so it holds even if the server code had a bug.

USE master;
GO

IF SUSER_ID(N'$(LoginName)') IS NULL
    CREATE LOGIN [$(LoginName)] WITH PASSWORD = N'$(LoginPassword)', DEFAULT_DATABASE = [$(DatabaseName)], CHECK_POLICY = ON;
ELSE
    ALTER LOGIN [$(LoginName)] WITH PASSWORD = N'$(LoginPassword)', DEFAULT_DATABASE = [$(DatabaseName)], CHECK_POLICY = ON;
ALTER LOGIN [$(LoginName)] ENABLE;

IF CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS INT) = 1
    PRINT N'WARNING: this server only allows Windows authentication, so the login cannot be used yet. '
        + N'In SSMS: right-click the server > Properties > Security > "SQL Server and Windows Authentication mode", '
        + N'then restart the SQL Server service.';
GO

USE [$(DatabaseName)];
GO

IF USER_ID(N'$(LoginName)') IS NULL
    CREATE USER [$(LoginName)] FOR LOGIN [$(LoginName)];
GO

-- Schema-level rights also cover tables created later.
GRANT SELECT, INSERT, UPDATE ON SCHEMA::[$(SchemaName)] TO [$(LoginName)];
DENY DELETE ON SCHEMA::[$(SchemaName)] TO [$(LoginName)];
GRANT EXECUTE ON OBJECT::[$(SchemaName)].CreateTable TO [$(LoginName)];
GO

PRINT N'Done. Test the login with: sqlcmd -S localhost -U $(LoginName) -P "<password>" -d $(DatabaseName) -C -Q "SELECT 1"';
GO

SET NOEXEC OFF;
GO
