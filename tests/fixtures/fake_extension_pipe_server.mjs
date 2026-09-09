import net from 'node:net';

const [endpoint, sessionId, connectionId, authToken] = process.argv.slice(2);
if (!endpoint || !sessionId || !connectionId || !authToken) throw new Error('missing named-pipe fixture arguments');

const protocol = 'omp-deck-fallback-v1';
const line = value => `${JSON.stringify(value)}\n`;
const server = net.createServer(socket => {
  let authenticated = false;
  let input = '';
  socket.on('data', chunk => {
    input += chunk.toString('utf8');
    let newline = input.indexOf('\n');
    while (newline >= 0) {
      const request = JSON.parse(input.slice(0, newline));
      input = input.slice(newline + 1);
      if (!authenticated) {
        if (
          request.protocol !== protocol
          || request.version !== 1
          || request.type !== 'connect'
          || request.token !== authToken
        ) {
          socket.destroy();
          return;
        }
        authenticated = true;
        socket.write(line({
          protocol,
          version: 1,
          type: 'registration',
          ompVersion: 'test',
          compatible: true,
          connectionId,
          session: {id: sessionId, file: null, title: 'Pipe test'},
          sessionId,
          title: 'Pipe test',
          project: 'C:\\work\\pipe-test',
          capabilities: {
            sendMessage: true,
            followUp: true,
            interrupt: true,
            shutdown: true,
            todos: true,
            subagents: true,
            answerUiRequest: false,
            answer_ui_request: false,
          },
        }));
        socket.write(line({
          protocol,
          type: 'event',
          sequence: 1,
          kind: 'snapshot',
          data: {idle: true, pendingMessages: false, model: null, async: []},
        }));
        newline = input.indexOf('\n');
        continue;
      }
      const response = {protocol, type: 'response', id: request.id, ok: true};
      if (request.action === 'snapshot') {
        response.result = {idle: true, pendingMessages: false, model: null, async: [], eventSequence: 1};
      }
      socket.write(line(response));
      newline = input.indexOf('\n');
    }
  });
});

server.listen(endpoint, () => console.log('READY'));
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => server.close(() => process.exit(0)));
}
