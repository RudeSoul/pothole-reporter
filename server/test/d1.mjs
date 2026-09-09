import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";

class PreparedStatement {
  constructor(database, sql) {
    this.database = database;
    this.sql = sql;
    this.values = [];
  }

  bind(...values) {
    this.values = values;
    return this;
  }

  async first(column) {
    const row = this.database.prepare(this.sql).get(...this.values);
    if (column !== undefined) return row == null ? null : row[column];
    return row || null;
  }

  async all() {
    return {
      success: true,
      results: this.database.prepare(this.sql).all(...this.values),
    };
  }

  executeRun() {
    const result = this.database.prepare(this.sql).run(...this.values);
    return {
      success: true,
      meta: {
        changes: Number(result.changes || 0),
        last_row_id: result.lastInsertRowid == null
          ? null : Number(result.lastInsertRowid),
      },
    };
  }

  async run() {
    return this.executeRun();
  }
}

export class MemoryD1 {
  constructor(schemaPath) {
    this.sqlite = new DatabaseSync(":memory:");
    this.sqlite.exec(readFileSync(schemaPath, "utf8"));
    this.batchFailurePlan = null;
  }

  prepare(sql) {
    return new PreparedStatement(this.sqlite, sql);
  }

  exec(sql) {
    this.sqlite.exec(sql);
  }

  failNextBatchAt(index) {
    this.failBatchAfter(0, index);
  }

  failBatchAfter(successfulBatches, index) {
    this.batchFailurePlan = {
      successfulBatches: Math.max(0, Number(successfulBatches) || 0),
      index,
    };
  }

  async batch(statements) {
    let failureIndex = null;
    if (this.batchFailurePlan) {
      if (this.batchFailurePlan.successfulBatches > 0) {
        this.batchFailurePlan.successfulBatches--;
      } else {
        failureIndex = this.batchFailurePlan.index;
        this.batchFailurePlan = null;
      }
    }
    this.sqlite.exec("BEGIN IMMEDIATE");
    try {
      const results = [];
      for (let index = 0; index < statements.length; index++) {
        if (index === failureIndex) {
          throw new Error(`Injected D1 batch failure at statement ${index}`);
        }
        results.push(statements[index].executeRun());
      }
      this.sqlite.exec("COMMIT");
      return results;
    } catch (error) {
      this.sqlite.exec("ROLLBACK");
      throw error;
    }
  }

  close() {
    this.sqlite.close();
  }
}

export class MemoryKV {
  constructor() {
    this.values = new Map();
  }

  async get(key) {
    const value = this.values.get(key);
    if (!value) return null;
    if (value.expiresAt && value.expiresAt <= Date.now()) {
      this.values.delete(key);
      return null;
    }
    return value.data;
  }

  async put(key, data, options = {}) {
    this.values.set(key, {
      data,
      expiresAt: options.expirationTtl
        ? Date.now() + options.expirationTtl * 1000 : null,
    });
  }
}
