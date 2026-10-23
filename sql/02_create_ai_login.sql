-- Step 2 of 2: creates the login the MCP server uses, with only the rights it needs.
-- Run as an administrator after 01_create_database.sql. Safe to run again: it then resets the password.
--
-- What ari_mcp can do:   read, insert and update rows in tables in the "ai" schema,
--                        and create new tables there through ai.CreateTable.
-- What it cannot do:     DELETE, DROP, TRUNCATE or ALTER anything, create tables any other way,
--                        or touch anything outside the "ai" schema.
-- SQL Server enforces this itself, so it holds even if the server code had a bug.
--
-- Before running: put your own password on the line below, and use the same one in the server's
-- connection string (ARI_MEMORY_CONNECTION_STRING). SQL Server requires at least 8 characters with three of:
-- upper case, lower case, digits and symbols, and the password may not contain the login name.
-- A ' in the password is written as '' inside the quotes.

DECLARE @Password NVARCHAR(128) = N'CHANGE_ME_Str0ng!Pass';

IF @Password LIKE N'CHANGE[_]ME%'
BEGIN
    RAISERROR(N'Set your own password at the top of 02_create_ai_login.sql before running it.', 16, 1);
    SET NOEXEC ON;  -- Skip the rest of the script.
END;

USE master;

-- CREATE LOGIN and ALTER LOGIN don't accept variables, so the statement is built with the password quoted.
DECLARE @sql NVARCHAR(MAX) =
    CASE WHEN SUSER_ID(N'ari_mcp') IS NULL THEN N'CREATE' ELSE N'ALTER' END
    + N' LOGIN ari_mcp WITH PASSWORD = N' + QUOTENAME(@Password, N'''')
    + N', DEFAULT_DATABASE = ArisMemory, CHECK_POLICY = ON;';
EXEC sys.sp_executesql @sql;
ALTER LOGIN ari_mcp ENABLE;

IF CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS INT) = 1
    PRINT N'WARNING: this server only allows Windows authentication, so ari_mcp cannot log in yet. '
        + N'In SSMS: right-click the server > Properties > Security > "SQL Server and Windows Authentication mode", '
        + N'then restart the SQL Server service.';
GO

USE ArisMemory;
GO

IF USER_ID(N'ari_mcp') IS NULL
    CREATE USER ari_mcp FOR LOGIN ari_mcp;
GO

-- Schema-level rights also cover tables created later.
GRANT SELECT, INSERT, UPDATE ON SCHEMA::ai TO ari_mcp;
DENY DELETE ON SCHEMA::ai TO ari_mcp;
GRANT EXECUTE ON OBJECT::ai.CreateTable TO ari_mcp;
GO

PRINT N'Done. Test the login with: sqlcmd -S localhost -U ari_mcp -P "<password>" -d ArisMemory -C -Q "SELECT 1"';
GO

SET NOEXEC OFF;
GO
