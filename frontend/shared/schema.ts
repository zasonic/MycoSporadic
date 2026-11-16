import { sqliteTable, text, integer } from "drizzle-orm/sqlite-core";
import { createInsertSchema } from "drizzle-zod";
import type * as z from "zod/mini";

export const users = sqliteTable("users", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  username: text("username").notNull().unique(),
  password: text("password").notNull(),
});

export const insertUserSchema = createInsertSchema(users).pick({
  username: true,
  password: true,
});

export type InsertUser = z.infer<typeof insertUserSchema>;
export type User = typeof users.$inferSelect;

// Frontend demonstration state. No device commands are sent.
export type Zone = {
  id: number; name: string; stage: string; temperature: number;
  humidity: number; substrate: number; co2: number | null;
  humidityLow: number; humidityHigh: number; tempLow: number; tempHigh: number;
  outputs: { humidifier: boolean; fan: boolean; heater: boolean };
};
