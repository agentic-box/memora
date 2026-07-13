import { databaseCatalog, type DatabaseEnv } from "./_db";

interface Env extends DatabaseEnv {}

export const onRequestGet: PagesFunction<Env> = async ({ env }) => {
  const catalog = databaseCatalog(env);
  return Response.json({
    databases: catalog.databases,
    default: catalog.defaultDatabase,
  });
};
