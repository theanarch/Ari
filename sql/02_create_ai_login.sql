-- Creates the login the MCP server uses, with only the rights it needs. Safe to run again.
--
-- What ari_mcp can do:   read, insert and update rows in tables in the "ai" schema,
--                        and create new tables there through ai.CreateTable.
-- What it cannot do:     DELETE, DROP, TRUNCATE or ALTER anything, create tables any other way,
--                        or touch anything outside the "ai" schema.
-- SQL Server enforces this itself, so it holds even if the server code had a bug.
--
-- Requires "SQL Server and Windows Authentication mode" on the server
-- (SSMS: right-click the server > Properties > Security), then a restart of the SQL Server service.
-- Change the password before running, and use the same password in the connection string.

USE master;
GO

-- Running the script again resets the password, so the login always matches what this file says.
IF SUSER_ID(N'ari_mcp') IS NULL
    CREATE LOGIN ari_mcp WITH PASSWORD = N'CHANGE_ME_Str0ng!Pass', DEFAULT_DATABASE = ArisMemory, CHECK_POLICY = ON;
ELSE
    ALTER LOGIN ari_mcp WITH PASSWORD = N'CHANGE_ME_Str0ng!Pass', DEFAULT_DATABASE = ArisMemory, CHECK_POLICY = ON;
ALTER LOGIN ari_mcp ENABLE;
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
