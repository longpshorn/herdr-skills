// npm run dev: serves on this checkout's own port (.env.local, written by bootstrap), so worktrees never collide.
import { createServer } from 'node:http';
import { DURATIONS } from './durations.ts';

const port = Number(process.env.PORT ?? 3000);
const server = createServer((_request, response) => response.end('ok\n'));
server.requestTimeout = DURATIONS.requestTimeoutMs;
server.listen(port, () => console.log(`listening on http://localhost:${port}`));
