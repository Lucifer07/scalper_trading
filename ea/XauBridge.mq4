//+------------------------------------------------------------------+
//| XauBridge.mq4 — xau-monitor MT4 bridge (EA sebagai TCP client)   |
//| Salin XauBridge.mq4 + XauBridgeCommands.mqh ke MQL4/Experts/,    |
//| centang "Allow DLL imports", biarkan InpAllowTrading=false untuk |
//| mode read-only. Protokol & arming: docs/mt4-integration.md       |
//| Port/magic/allow per-simbol dibaca dari MQL4/Files/xaubridge.ini |
//| (otoritas atas input chart; MT4 sering me-reset input chart).     |
//+------------------------------------------------------------------+
#property copyright "xau-monitor"
#property version   "1.00"
#property strict

input string InpHost              = "127.0.0.1";
input int    InpPort              = 8770;
input bool   InpAllowTrading      = false;
input int    InpMagic             = 20261116;
input int    InpMaxSlippagePoints = 30;
input double InpMaxLot            = 0.10;
input int    InpMaxSpreadPoints   = 100;
input bool   InpVerbose           = true;

#import "ws2_32.dll"
int WSAStartup(int version, int &data[]);
int WSACleanup();
int socket(int af, int type, int protocol);
int connect(int s, uchar &addr[], int len);
int send(int s, uchar &buf[], int len, int flags);
int recv(int s, uchar &buf[], int len, int flags);
int closesocket(int s);
int select(int nfds, int &readfds[], int &writefds[], int &exceptfds[], int &timeout[]);
int WSAGetLastError();
#import

int    g_sock     = -1;
int    g_attempts = 0;
int    g_frames   = 0;
string g_rx       = "";
int    g_port     = InpPort;
int    g_magic    = InpMagic;
bool   g_allow    = InpAllowTrading;

#include "XauBridgeCommands.mqh"

//+------------------------------------------------------------------+
//| Konfigurasi per-simbol dari MQL4/Files/xaubridge.ini.            |
//| Format baris: SIMBOL=port,magic,allow(0/1)  (# = komentar).      |
//| Input chart tetap fallback; ini otoritatif bila entri simbol ada.|
//+------------------------------------------------------------------+
void LoadSymbolConfig()
{
   int h = FileOpen("xaubridge.ini", FILE_READ | FILE_TXT | FILE_ANSI);
   if(h == INVALID_HANDLE) return;
   string sym = Symbol();
   while(!FileIsEnding(h))
   {
      string line = FileReadString(h);
      StringTrimLeft(line);
      StringTrimRight(line);
      if(StringLen(line) == 0 || StringGetCharacter(line, 0) == '#') continue;
      int eq = StringFind(line, "=");
      if(eq <= 0) continue;
      if(StringSubstr(line, 0, eq) != sym) continue;
      string v[];
      if(StringSplit(StringSubstr(line, eq + 1), ',', v) < 2) continue;
      int port  = (int)StringToInteger(v[0]);
      int magic = (int)StringToInteger(v[1]);
      if(port <= 0 || port > 65535) { PrintFormat("xaubridge.ini: port invalid untuk %s", sym); continue; }
      g_port  = port;
      g_magic = magic;
      if(ArraySize(v) >= 3) g_allow = (StringToInteger(v[2]) != 0);
      PrintFormat("XauBridge config ini: %s port=%d magic=%d trading=%s",
                  sym, g_port, g_magic, g_allow ? "ON" : "OFF");
      break;
   }
   FileClose(h);
}

int OnInit()
{
   EventSetMillisecondTimer(200);
   LoadSymbolConfig();
   PrintFormat("XauBridge init host=%s port=%d trading=%s magic=%d",
               InpHost, g_port, g_allow ? "ON" : "OFF", g_magic);
   TryConnect();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Drop("deinit");
   WSACleanup();
}

// IO hanya di timer: OnTick tidak boleh diblokir oleh socket.
void OnTick() {}

void OnTimer()
{
   if(g_sock < 0) { TryConnect(); return; }
   if(!Readable(g_sock)) return;
   uchar buf[8192];
   int n = recv(g_sock, buf, 8192, 0);
   if(n <= 0) { Drop("recv<=0 err=" + IntegerToString(WSAGetLastError())); return; }
   g_rx += CharArrayToString(buf, 0, n, CP_UTF8);
   int cut = StringFind(g_rx, "\n");
   while(cut >= 0)
   {
      string line = StringSubstr(g_rx, 0, cut);
      g_rx = StringSubstr(g_rx, cut + 1);
      if(StringLen(line) > 0) HandleLine(line);
      cut = StringFind(g_rx, "\n");
   }
   if(StringLen(g_rx) > 32000) Drop("rx overflow");
}

void TryConnect()
{
   if(g_sock >= 0) return;
   // WSADATA ~400 byte: buffer 16 int (64 byte) MENIMPA stack -> terminal crash.
   int wsa[256];
   ArrayInitialize(wsa, 0);
   if(WSAStartup(0x0202, wsa) != 0)
   {
      PrintFormat("WSAStartup gagal err=%d", WSAGetLastError());
      return;
   }
   int s = socket(2, 1, 6);              // AF_INET, SOCK_STREAM, IPPROTO_TCP
   if(s < 0) { PrintFormat("socket() gagal err=%d", WSAGetLastError()); return; }
   string octets[];
   if(StringSplit(InpHost, '.', octets) != 4)
   {
      Print("InpHost harus IPv4 literal (mis. 127.0.0.1)");
      closesocket(s);
      return;
   }
   // sockaddr_in 16 byte dibangun eksplisit: endianness tidak boleh ditebak.
   uchar sa[16];
   ArrayInitialize(sa, 0);
   sa[0] = 2; sa[1] = 0;                 // sin_family = AF_INET (host order)
   sa[2] = (uchar)((g_port >> 8) & 0xFF);
   sa[3] = (uchar)(g_port & 0xFF);      // sin_port = network order
   for(int i = 0; i < 4; i++) sa[4 + i] = (uchar)StringToInteger(octets[i]);
   if(connect(s, sa, 16) != 0)
   {
      closesocket(s);
      g_attempts++;
      if(InpVerbose && g_attempts % 30 == 1)
         PrintFormat("connect gagal err=%d (server Python belum jalan?)", WSAGetLastError());
      return;
   }
   g_sock = s;
   g_rx = "";
   g_attempts = 0;
   Print("XauBridge terhubung ke server Python");
}

bool Readable(const int s)
{
   int rfds[65];  ArrayInitialize(rfds, 0);
   int wfds[1];   wfds[0] = 0;
   int efds[1];   efds[0] = 0;
   int tv[2];     tv[0] = 0; tv[1] = 0;  // polling: tidak menunggu
   rfds[0] = 1;   rfds[1] = s;
   int rc = select(0, rfds, wfds, efds, tv);
   if(rc < 0) { Drop("select err=" + IntegerToString(WSAGetLastError())); return false; }
   return (rc > 0 && rfds[0] > 0);
}

void Drop(const string why)
{
   if(g_sock >= 0)
   {
      closesocket(g_sock);
      g_sock = -1;
      g_rx = "";
      PrintFormat("XauBridge putus: %s", why);
   }
}

void HandleLine(const string line)
{
   string parts[];
   int n = StringSplit(line, '|', parts);
   if(n < 2) { PrintFormat("frame rusak: %s", line); return; }
   g_frames++;
   string id = parts[0];
   string cmd = parts[1];
   string ps = (n >= 3) ? parts[2] : "";
   if(cmd == "ping")         { CmdPing(id); return; }
   if(cmd == "symbol_info")  { CmdSymbolInfo(id, ps); return; }
   if(cmd == "rates")        { CmdRates(id, ps); return; }
   if(cmd == "positions")    { CmdPositions(id, ps); return; }
   if(cmd == "pending_list") { CmdPendingList(id, ps); return; }
   if(cmd == "order_send")   { CmdOrderSend(id, ps); return; }
   if(cmd == "order_delete") { CmdOrderDelete(id, ps); return; }
   if(cmd == "order_close")  { CmdOrderClose(id, ps); return; }
   if(cmd == "order_modify") { CmdOrderModify(id, ps); return; }
   Reply(id, false, "err=unknown_cmd;msg=" + Sane(cmd));
}
