> 基于 edge-tts和 cf workers创建 在线配音网站，及兼容OpenAI TTS 的接口

 
玩配音的基本都知道，微软的edge-tts是好用免费的语音合成利器，唯一缺点是对国内限流越来越严，不过可以通过部署到 cloudflare 来规避，并且还能白嫖 cloudflare的服务器和带宽资源。

先看效果，完成后将有一个配音api接口和一个web配音界面


![image.png](https://pyvideotrans.com/img/20241228183550-0.webp)

这是web界面


```js

const requestBody = {
          "model": "tts-1",
          "input": '这是要合成语音的文字',
          "voice": 'zh-CN-XiaoxiaoNeural',
          "response_format": "mp3",
          "speed": 1.0
        };

const response = await fetch('部署到cloudflare后的网址', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'Authorization': `Bearer 部署后的key，随意`,
            },
            body: JSON.stringify(requestBody),
});
          
          
```
这是接口调用js版函数，并兼容 openai tts 接口


接下来说说如何部署到 cloudflare 上


## API（兼容 OpenAI）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/audio/speech` | 合成语音，参数同 OpenAI `audio.speech.create` |
| GET | `/v1/models`、`/v1/models/{id}` | 模型列表 / 详情 |
| GET | `/v1/audio/voices?locale=zh-CN` | 扩展：可用音色列表（含风格 styles） |
| POST | `/openai-fm/v1/audio/speech` | 旧路由，强制使用 openai.fm 后端 |

`POST /v1/audio/speech` 参数：

- `input`（必填）：要合成的文本，最长 50000 字符，超长会自动分段并发合成。行尾写 `[500]` 表示停顿 500ms。
- `model`：`kokoro`（Hugging Face Space，见下文），`tts-1` / `edge-tts`（Edge TTS），`tts-1-hd`（Edge TTS，mp3 为 48kHz 192kbps），`gpt-4o-mini-tts` / `openai-fm`（openai.fm）。未知模型名按 Edge 处理。
- `voice`：Edge 音色名（如 `zh-CN-XiaoxiaoNeural`），或 OpenAI 音色名 `alloy`、`ash`、`ballad`、`coral`、`echo`、`fable`、`onyx`、`nova`、`sage`、`shimmer`、`verse`、`marin`、`cedar`（映射到 Edge 的英文/多语言音色）。
- `response_format`：`mp3`（默认）、`opus`、`wav`、`pcm`（24kHz 16bit 单声道，与 OpenAI 一致）。Edge 不支持 `aac`、`flac`。
- `speed`：0.25–4.0。
- `stream_format`：`audio`（默认，边合成边返回）或 `sse`（`speech.audio.delta` / `speech.audio.done` 事件）。
- `instructions`：openai.fm 后端的语气提示词。
- 扩展参数（仅 Edge）：`style`（如 `cheerful`）、`pitch`（Hz）、`volume`（1.0 = +100%）。

错误响应为 OpenAI 格式：`{"error": {"message", "type", "param", "code"}}`，并带有对应的 HTTP 状态码（400/401/404/405/429/502）。

### 用 wrangler 部署

```bash
npm install
npx wrangler secret put API_KEY   # 可选，多个 key 用英文逗号分隔；不设置则不校验
npx wrangler deploy
```

鉴权方式：`Authorization: Bearer <key>` 或 `x-api-key: <key>`。

### 两个免费的 Edge 接口（自动切换）

本项目只使用免费、无需注册的服务。Edge TTS 有两个入口：

| 接口 | 格式 | 风格 style | `[500]` 停顿 | 音色数 |
|---|---|---|---|---|
| 微软翻译 App token 接口（主） | mp3 / opus / wav / pcm | 支持 | 支持 | 约 840 |
| Edge 浏览器“大声朗读” WebSocket（备用） | 仅 mp3 | 不支持（被服务端拒绝） | 忽略 | 约 320 |

默认 `auto`：先走主接口，失败（5xx/网络错误）时 mp3 请求自动改走备用接口。可用环境变量 `EDGE_ENDPOINT` 固定为 `translator` 或 `readaloud`（在 `wrangler.toml` 的 `[vars]` 或控制台里设置）。

### 可选：Kokoro 开源模型（部署在 Hugging Face 免费 Space）

`hf-space/` 目录是一个兼容 OpenAI 的 [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) 服务（Apache-2.0，免费 CPU Space 即可运行），共 157 个音色，其中中文 108 个（`zf_xiaoxiao`、`zm_yunxi`，以及 v1.1-zh 的 `zf_001` … `zm_100`）。

1. 在 https://huggingface.co/new-space 新建 Space，SDK 选 **Docker**，硬件选免费的 **CPU basic**。
2. 上传 `hf-space/` 里的 4 个文件：`huggingface-cli upload <用户名>/<space名> hf-space . --repo-type=space`
3. （可选）Space 设置里添加 Secret `API_KEY`。
4. Worker 中设置 `KOKORO_URL=https://<用户名>-<space名>.hf.space`（有 key 时再设置 `KOKORO_API_KEY`）。

之后 `model: "kokoro"`，或直接使用 Kokoro 音色名（如 `"voice": "zf_xiaoxiao"`），就会转发到 Space；`GET /v1/audio/voices?model=kokoro` 列出其音色。

注意：免费 Space 48 小时无访问会休眠，唤醒需 1–2 分钟；2 核 CPU 合成速度约为实时的 1–1.5 倍，适合短文本或备用，长文本仍推荐 Edge。


> 注意：openai.fm 是 OpenAI 的演示站，目前对程序化访问返回 Vercel 安全验证（429），该后端基本不可用。

## 登录 cloudflare 创建一个Workers（旧版：在线编辑器粘贴代码）

> 下面粘贴的代码是旧版本，推荐直接使用 `src/index.ts` 通过 wrangler 部署。


> 网址 https://dash.cloudflare.com/   如何登录注册不再赘述

登录后，点击左侧 `Workers 和 Pages`，打开创建页面



![image.png](https://pyvideotrans.com/img/20241228183551-1.webp)

继续点击创建

![image.png](https://pyvideotrans.com/img/20241228183552-2.webp)

然后在出现的输入框中填写一个英文名称，作为cloudflare赠送的免费子域名头



![image.png](https://pyvideotrans.com/img/20241228183552-3.webp)

点击右下角部署后，在新出现的页面中继续点击`编辑代码`，进入核心阶段，复制代码


![image.png](https://pyvideotrans.com/img/20241228183552-4.webp)

然后删掉里面所有的代码，复制下面的代码去替换

![image.png](https://pyvideotrans.com/img/20241228183553-5.webp)

```
// 自定义api key ，用于防止滥用
const API_KEY = '';
const encoder = new TextEncoder();
let expiredAt = null;
let endpoint = null;
let clientId = "";


const TOKEN_REFRESH_BEFORE_EXPIRY = 3 * 60;  
let tokenInfo = {
    endpoint: null,
    token: null,
    expiredAt: null
};

addEventListener("fetch", event => {
    event.respondWith(handleRequest(event.request));
});

async function handleRequest(request) {
    if (request.method === "OPTIONS") {
        return handleOptions(request);
    }
    
  
    const authHeader = request.headers.get("authorization") || request.headers.get("x-api-key");
    const apiKey = authHeader?.startsWith("Bearer ") 
        ? authHeader.slice(7) 
        : null;

    // 只在设置了 API_KEY 的情况下才验证              
    if (API_KEY && apiKey !== API_KEY) {
        return new Response(JSON.stringify({
            error: {
                message: "Invalid API key. Use 'Authorization: Bearer your-api-key' header",
                type: "invalid_request_error",
                param: null,
                code: "invalid_api_key"
            }
        }), {
            status: 401,
            headers: {
                "Content-Type": "application/json",
                ...makeCORSHeaders()
            }
        });
    }

    const requestUrl = new URL(request.url);
    const path = requestUrl.pathname;
    
    if (path === "/v1/audio/speech") {
        try {
            const requestBody = await request.json();
            const { 
                model = "tts-1",
                input,
                voice = "zh-CN-XiaoxiaoNeural",
                response_format = "mp3",
                speed = '1.0',
                volume='0',
                pitch = '0', // 添加 pitch 参数，默认值为 0
                style = "general"//添加style参数，默认值为general
            } = requestBody;

            let rate = parseInt(String( (parseFloat(speed)-1.0)*100) );
            let numVolume = parseInt( String(parseFloat(volume)*100) );
            let numPitch = parseInt(pitch); 
            const response = await getVoice(
                input, 
                voice, 
                rate>=0?`+${rate}%`:`${rate}%`,
                numPitch>=0?`+${numPitch}Hz`:`${numPitch}Hz`,
                numVolume>=0?`+${numVolume}%`:`${numVolume}%`,
                style,
                "audio-24khz-48kbitrate-mono-mp3"
            );

            return response;

        } catch (error) {
            console.error("Error:", error);
            return new Response(JSON.stringify({
                error: {
                    message: error.message,
                    type: "api_error",
                    param: null,
                    code: "edge_tts_error"
                }
            }), {
                status: 500,
                headers: {
                    "Content-Type": "application/json",
                    ...makeCORSHeaders()
                }
            });
        }
    }

    // 默认返回 404
    return new Response("Not Found", { status: 404 });
}

async function handleOptions(request) {
    return new Response(null, {
        status: 204,
        headers: {
            ...makeCORSHeaders(),
            "Access-Control-Allow-Methods": "GET,HEAD,POST,OPTIONS",
            "Access-Control-Allow-Headers": request.headers.get("Access-Control-Request-Headers") || "Authorization"
        }
    });
}

async function getVoice(text, voiceName = "zh-CN-XiaoxiaoNeural", rate = '+0%', pitch = '+0Hz', volume='+0%',style = "general", outputFormat = "audio-24khz-48kbitrate-mono-mp3") {
    try {
        const maxChunkSize = 2000;  
        const chunks = text.trim().split("\n");


        // 获取每个分段的音频
        //const audioChunks = await Promise.all(chunks.map(chunk => getAudioChunk(chunk, voiceName, rate, pitch, volume,style, outputFormat)));
        let audioChunks=[]
        while(chunks.length>0){
            try{
                let audio_chunk= await getAudioChunk(chunks.shift(), voiceName, rate, pitch, volume,style, outputFormat)
                audioChunks.push(audio_chunk)

            }catch(e){
                return new Response(JSON.stringify({
                    error: {
                        message: String(e),
                        type: "api_error",
                        param: `${voiceName}, ${rate}, ${pitch}, ${volume},${style}, ${outputFormat}`,
                        code: "edge_tts_error"
                    }
                }), {
                    status: 500,
                    headers: {
                        "Content-Type": "application/json",
                        ...makeCORSHeaders()
                    }
                });

            }
        }
       

        // 将音频片段拼接起来
        const concatenatedAudio = new Blob(audioChunks, { type: 'audio/mpeg' });
        const response = new Response(concatenatedAudio, {
            headers: {
                "Content-Type": "audio/mpeg",
                ...makeCORSHeaders()
            }
        });

        
        return response;

    } catch (error) {
        console.error("语音合成失败:", error);
        return new Response(JSON.stringify({
            error: {
                message: error,
                type: "api_error",
                param: null,
                code: "edge_tts_error "+voiceName
            }
        }), {
            status: 500,
            headers: {
                "Content-Type": "application/json",
                ...makeCORSHeaders()
            }
        });
    }
}



//获取单个音频数据
async function getAudioChunk(text, voiceName, rate, pitch,volume, style, outputFormat='audio-24khz-48kbitrate-mono-mp3') {
    const endpoint = await getEndpoint();
    const url = `https://${endpoint.r}.tts.speech.microsoft.com/cognitiveservices/v1`;
    let m=text.match(/\[(\d+)\]\s*?$/);
    let slien=0;
    if(m&&m.length==2){
      slien=parseInt(m[1]);
      text=text.replace(m[0],'')

    }
    const response = await fetch(url, {
        method: "POST",
        headers: {
            "Authorization": endpoint.t,
            "Content-Type": "application/ssml+xml",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0",
            "X-Microsoft-OutputFormat": outputFormat
        },
        body: getSsml(text, voiceName, rate,pitch,volume, style,slien)
    });

    if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`Edge TTS API error: ${response.status} ${errorText}`);
    }

    return response.blob();

}

function getSsml(text, voiceName, rate, pitch,volume,style,slien=0) {
   let slien_str='';
   if(slien>0){
    slien_str=`<break time="${slien}ms" />`
   }
    return `<speak xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" version="1.0" xml:lang="zh-CN"> 
                <voice name="${voiceName}"> 
                    <mstts:express-as style="${style}"  styledegree="2.0" role="default" > 
                        <prosody rate="${rate}" pitch="${pitch}" volume="${volume}">${text}</prosody> 
                    </mstts:express-as> 
                    ${slien_str}
                </voice> 
            </speak>`;

}

async function getEndpoint() {
    const now = Date.now() / 1000;
    
    if (tokenInfo.token && tokenInfo.expiredAt && now < tokenInfo.expiredAt - TOKEN_REFRESH_BEFORE_EXPIRY) {
        return tokenInfo.endpoint;
    }

    // 获取新token
    const endpointUrl = "https://dev.microsofttranslator.com/apps/endpoint?api-version=1.0";
    const clientId = crypto.randomUUID().replace(/-/g, "");
    
    try {
        const response = await fetch(endpointUrl, {
            method: "POST",
            headers: {
                "Accept-Language": "zh-Hans",
                "X-ClientVersion": "4.0.530a 5fe1dc6c",
                "X-UserId": "0f04d16a175c411e",
                "X-HomeGeographicRegion": "zh-Hans-CN",
                "X-ClientTraceId": clientId,
                "X-MT-Signature": await sign(endpointUrl),
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 Edg/127.0.0.0",
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": "0",
                "Accept-Encoding": "gzip"
            }
        });

        if (!response.ok) {
            throw new Error(`获取endpoint失败: ${response.status}`);
        }

        const data = await response.json();
        const jwt = data.t.split(".")[1];
        const decodedJwt = JSON.parse(atob(jwt));
        
        tokenInfo = {
            endpoint: data,
            token: data.t,
            expiredAt: decodedJwt.exp
        };

        return data;

    } catch (error) {
        console.error("获取endpoint失败:", error);
        // 如果有缓存的token，即使过期也尝试使用
        if (tokenInfo.token) {
            console.log("使用过期的缓存token");
            return tokenInfo.endpoint;
        }
        throw error;
    }
}

function addCORSHeaders(response) {
    const newHeaders = new Headers(response.headers);
    for (const [key, value] of Object.entries(makeCORSHeaders())) {
        newHeaders.set(key, value);
    }
    return new Response(response.body, { ...response, headers: newHeaders });
}

function makeCORSHeaders() {
    return {
        "Access-Control-Allow-Origin": "*", 
        "Access-Control-Allow-Methods": "GET,HEAD,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, x-api-key",
        "Access-Control-Max-Age": "86400"  
    };
}

async function hmacSha256(key, data) {
    const cryptoKey = await crypto.subtle.importKey(
        "raw",
        key,
        { name: "HMAC", hash: { name: "SHA-256" } },
        false,
        ["sign"]
    );
    const signature = await crypto.subtle.sign("HMAC", cryptoKey, new TextEncoder().encode(data));
    return new Uint8Array(signature);
}

async function base64ToBytes(base64) {
    const binaryString = atob(base64);
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i++) {
        bytes[i] = binaryString.charCodeAt(i);
    }
    return bytes;
}

async function bytesToBase64(bytes) {
    return btoa(String.fromCharCode.apply(null, bytes));
}

function uuid() {
    return crypto.randomUUID().replace(/-/g, "");
}

async function sign(urlStr) {
    const url = urlStr.split("://")[1];
    const encodedUrl = encodeURIComponent(url);
    const uuidStr = uuid();
    const formattedDate = dateFormat();
    const bytesToSign = `MSTranslatorAndroidApp${encodedUrl}${formattedDate}${uuidStr}`.toLowerCase();
    const decode = await base64ToBytes("oik6PdDdMnOXemTbwvMn9de/h9lFnfBaCWbGMMZqqoSaQaqUOqjVGm5NqsmjcBI1x+sS9ugjB55HEJWRiFXYFw==");
    const signData = await hmacSha256(decode, bytesToSign);
    const signBase64 = await bytesToBase64(signData);
    return `MSTranslatorAndroidApp::${signBase64}::${formattedDate}::${uuidStr}`;
}

function dateFormat() {
    const formattedDate = (new Date()).toUTCString().replace(/GMT/, "").trim() + " GMT";
    return formattedDate.toLowerCase();
}

// 添加请求超时控制
async function fetchWithTimeout(url, options, timeout = 30000) {
    const controller = new AbortController();
    const id = setTimeout(() => controller.abort(), timeout);
    
    try {
        const response = await fetch(url, {
            ...options,
            signal: controller.signal
        });
        clearTimeout(id);
        return response;
    } catch (error) {
        clearTimeout(id);
        throw error;
    }
}
```

**特别需要注意的是顶部两行代码，设置 api key ，防止被他人滥用**

```
// 这是 api key，用于验证可用权限
const API_KEY = '';
```

## 绑定自己的域名

默认绑定的域名是 `https://输入框填写的子域名头.你的账号名.workers.dev/`

但不幸的是该域名在国内被墙，想免翻墙使用，你需要绑定一个自己的域名。

1. 如果你还没有在 cloudflare上添加过自己的域名，可点击右上角`添加--现有域`，然后输入自己的域名

![image.png](https://pyvideotrans.com/img/20241228183553-6.webp)

2. 如果在cloudflare上已添加过域名，则点击左侧名称返回管理界面，添加自定义域名


![image.png](https://pyvideotrans.com/img/20241228183554-7.webp)

点击 设置--域和路由--添加

![image.png](https://pyvideotrans.com/img/20241228183554-8.webp)

再点击自定义域，然后填写已添加到 cloudflare 的域名的子域名，例如我的域名 `pyvideotrans.com` 已添加cloudflare，那么此处我可以填写 `ttsapi.pyvideotrans.com`

![image.png](https://pyvideotrans.com/img/20241228183555-9.webp)

如下图，添加完毕


![image.png](https://pyvideotrans.com/img/20241228183555-10.webp)

此处显示你添加的自定义域

![image.png](https://pyvideotrans.com/img/20241228183555-11.webp)


## 使用 openai sdk 测试

这是兼容openai 的接口，可使用openai sdk 直接测试，如下python代码


```
import logging
from openai import OpenAI
import json
import httpx

api_key = 'adgas213423235saeg'  # 替换为你的实际 API key
base_url = 'https://xxx.xxx.com/v1' # 替换为你的自定义域，默认加 /v1


client = OpenAI(
    api_key=api_key,
    base_url=base_url
)



data = {
    'model': 'tts-1',
    'input': '你好啊，亲爱的朋友们',
    'voice': 'zh-CN-YunjianNeural',
    'response_format': 'mp3',
    'speed': 1.0,
}


try:
    response = client.audio.speech.create(
       **data
    )
    with open('./test_openai.mp3', 'wb') as f:
        f.write(response.content)
    print("MP3 file saved successfully to test_openai.mp3")

except Exception as e:
    print(f"An error occurred: {e}")

```


## 搭建web界面

> 接口有了，那么如何搭建页面呢？

打开该项目 https://github.com/jianchang512/tts-pyvideotrans
下载解压，然后将其中的 `index.html/output.css/vue.js` 3个文件放在服务器目录下，访问 index.html 即可。


![image.png](https://pyvideotrans.com/img/20241228183556-12.webp)

**注意在 index.html 搜索 `https://ttsapi.pyvideotrans.com`, 改为你部署在 cloudflare 的自定义域，否则无法使用**



## 参考

1. [edge-tts-openai-cf-worker](https://github.com/linshenkx/edge-tts-openai-cf-worker)
2. [edge-tts](https://github.com/rany2/edge-tts/)
