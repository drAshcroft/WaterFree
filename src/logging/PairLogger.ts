import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";

export interface PairLoggerLike extends vscode.Disposable {
  readonly logFilePath: string;
  log(scope: string, message: string): void;
  show(preserveFocus?: boolean): void;
}

export class PairLogger implements PairLoggerLike {
  private readonly _channel: vscode.OutputChannel;
  private _fileLoggingFailed = false;

  constructor(
    public readonly logFilePath: string,
    channelName = "WaterFree",
  ) {
    this._channel = vscode.window.createOutputChannel(channelName);
    this._ensureLogDirectory();
    this._writeLine(`${this._timestamp()} [pair][log] --- extension session started ---`);
    this.log("log", `writing extension logs to ${this.logFilePath}`);
  }

  log(scope: string, message: string): void {
    this._writeLine(`${this._timestamp()} [pair][${scope}] ${message}`);
  }

  show(preserveFocus = false): void {
    this._channel.show(preserveFocus);
  }

  dispose(): void {
    this._channel.dispose();
  }

  private _writeLine(line: string): void {
    this._channel.appendLine(line);

    try {
      fs.appendFileSync(this.logFilePath, `${line}\n`, "utf-8");
    } catch (err) {
      if (this._fileLoggingFailed) {
        return;
      }

      this._fileLoggingFailed = true;
      const detail = err instanceof Error ? err.message : String(err);
      this._channel.appendLine(
        `${this._timestamp()} [pair][log] Failed to write log file ${this.logFilePath}: ${detail}`,
      );
    }
  }

  private _ensureLogDirectory(): void {
    try {
      fs.mkdirSync(path.dirname(this.logFilePath), { recursive: true });
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      this._channel.appendLine(
        `${this._timestamp()} [pair][log] Failed to create log directory for ${this.logFilePath}: ${detail}`,
      );
      this._fileLoggingFailed = true;
    }
  }

  private _timestamp(): string {
    return new Date().toISOString();
  }
}
