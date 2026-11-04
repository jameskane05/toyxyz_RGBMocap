#include <jni.h>
#include <android/log.h>
#include <dlfcn.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cmath>
#include <string>

#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, "rgbfacecap", __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, "rgbfacecap", __VA_ARGS__)

using XrFlags64 = uint64_t;
using XrTime = int64_t;
using XrDuration = int64_t;
using XrBool32 = uint32_t;
using XrStructureType = int32_t;
using XrResult = int32_t;
using XrPath = uint64_t;
struct XrInstance_T;
struct XrSession_T;
struct XrSpace_T;
struct XrActionSet_T;
struct XrAction_T;
struct XrFaceTracker2FB_T;
struct XrEyeTrackerFB_T;
struct XrPassthroughFB_T;
struct XrPassthroughLayerFB_T;
using XrInstance = XrInstance_T*;
using XrSession = XrSession_T*;
using XrSpace = XrSpace_T*;
using XrActionSet = XrActionSet_T*;
using XrAction = XrAction_T*;
using XrFaceTracker2FB = XrFaceTracker2FB_T*;
using XrEyeTrackerFB = XrEyeTrackerFB_T*;
using XrPassthroughFB = XrPassthroughFB_T*;
using XrPassthroughLayerFB = XrPassthroughLayerFB_T*;
using PFN_xrVoidFunction = void (*)();

enum {
    XR_SUCCESS = 0,
    XR_TYPE_INSTANCE_CREATE_INFO = 3,
    XR_TYPE_SYSTEM_GET_INFO = 4,
    XR_TYPE_SYSTEM_PROPERTIES = 5,
    XR_TYPE_SESSION_CREATE_INFO = 8,
    XR_TYPE_SESSION_BEGIN_INFO = 10,
    XR_TYPE_FRAME_END_INFO = 12,
    XR_TYPE_EVENT_DATA_BUFFER = 16,
    XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED = 18,
    XR_TYPE_ACTION_STATE_BOOLEAN = 23,
    XR_TYPE_ACTION_SET_CREATE_INFO = 28,
    XR_TYPE_ACTION_CREATE_INFO = 29,
    XR_TYPE_FRAME_WAIT_INFO = 33,
    XR_TYPE_REFERENCE_SPACE_CREATE_INFO = 37,
    XR_TYPE_FRAME_STATE = 44,
    XR_TYPE_FRAME_BEGIN_INFO = 46,
    XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING = 51,
    XR_TYPE_ACTION_STATE_GET_INFO = 58,
    XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO = 60,
    XR_TYPE_ACTIONS_SYNC_INFO = 61,
    XR_TYPE_LOADER_INIT_INFO_ANDROID_KHR = 1000089000,
    XR_TYPE_INSTANCE_CREATE_INFO_ANDROID_KHR = 1000008000,
    XR_TYPE_PASSTHROUGH_CREATE_INFO_FB = 1000118001,
    XR_TYPE_PASSTHROUGH_LAYER_CREATE_INFO_FB = 1000118002,
    XR_TYPE_COMPOSITION_LAYER_PASSTHROUGH_FB = 1000118003,
    XR_TYPE_FACE_TRACKER_CREATE_INFO2_FB = 1000287014,
    XR_TYPE_FACE_EXPRESSION_INFO2_FB = 1000287015,
    XR_TYPE_FACE_EXPRESSION_WEIGHTS2_FB = 1000287016,
    XR_TYPE_EYE_TRACKER_CREATE_INFO_FB = 1000202001,
    XR_TYPE_EYE_GAZES_INFO_FB = 1000202002,
    XR_TYPE_EYE_GAZES_FB = 1000202003,
    XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY = 1,
    XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO = 2,
    XR_REFERENCE_SPACE_TYPE_LOCAL = 2,
    XR_ENVIRONMENT_BLEND_MODE_OPAQUE = 1,
    XR_ENVIRONMENT_BLEND_MODE_ALPHA_BLEND = 3,
    XR_SESSION_STATE_READY = 2,
    XR_SESSION_STATE_STOPPING = 6,
    XR_SESSION_STATE_EXITING = 8,
    XR_SESSION_STATE_LOSS_PENDING = 7,
    XR_ACTION_TYPE_BOOLEAN_INPUT = 1,
    XR_FACE_EXPRESSION2_COUNT_FB = 70,
    XR_FACE_CONFIDENCE2_COUNT_FB = 2,
    XR_FACE_EXPRESSION_SET2_DEFAULT_FB = 0,
    XR_NULL_PATH = 0
};

struct XrVector3f { float x, y, z; };
struct XrQuaternionf { float x, y, z, w; };
struct XrPosef { XrQuaternionf orientation; XrVector3f position; };
struct XrApplicationInfo {
    char applicationName[128];
    uint32_t applicationVersion;
    char engineName[128];
    uint32_t engineVersion;
    uint64_t apiVersion;
};
struct XrLoaderInitInfoAndroidKHR {
    XrStructureType type; const void* next; void* applicationVM; void* applicationContext;
};
struct XrInstanceCreateInfoAndroidKHR {
    XrStructureType type; const void* next; void* applicationVM; void* applicationContext;
};
struct XrInstanceCreateInfo {
    XrStructureType type; const void* next; uint64_t createFlags; XrApplicationInfo applicationInfo;
    uint32_t enabledApiLayerCount; const char* const* enabledApiLayerNames;
    uint32_t enabledExtensionCount; const char* const* enabledExtensionNames;
};
struct XrSystemGetInfo { XrStructureType type; const void* next; int32_t formFactor; };
struct XrSystemProperties { XrStructureType type; void* next; uint64_t systemId; uint32_t vendorId; char systemName[256]; };
struct XrSessionCreateInfo { XrStructureType type; const void* next; uint64_t createFlags; uint64_t systemId; };
struct XrSessionBeginInfo { XrStructureType type; const void* next; int32_t primaryViewConfigurationType; };
struct XrReferenceSpaceCreateInfo { XrStructureType type; const void* next; int32_t referenceSpaceType; XrPosef poseInReferenceSpace; };
struct XrEventDataBuffer { XrStructureType type; const void* next; uint8_t varying[4000]; };
struct XrEventDataSessionStateChanged {
    XrStructureType type; const void* next; XrSession session; int32_t state; XrTime time;
};
struct XrFrameWaitInfo { XrStructureType type; const void* next; };
struct XrFrameState { XrStructureType type; void* next; XrTime predictedDisplayTime; XrDuration predictedDisplayPeriod; XrBool32 shouldRender; };
struct XrFrameBeginInfo { XrStructureType type; const void* next; };
struct XrCompositionLayerBaseHeader { XrStructureType type; const void* next; };
struct XrFrameEndInfo {
    XrStructureType type; const void* next; XrTime displayTime; int32_t environmentBlendMode;
    uint32_t layerCount; const XrCompositionLayerBaseHeader* const* layers;
};
struct XrFaceTrackerCreateInfo2FB {
    XrStructureType type; const void* next; int32_t faceExpressionSet;
    uint32_t requestedDataSourceCount; int32_t* requestedDataSources;
};
struct XrFaceExpressionInfo2FB { XrStructureType type; const void* next; XrTime time; };
struct XrFaceExpressionWeights2FB {
    XrStructureType type; void* next; uint32_t weightCount; float* weights;
    uint32_t confidenceCount; float* confidences; XrBool32 isValid;
    XrBool32 isEyeFollowingBlendshapesValid; int32_t dataSource; XrTime time;
};
struct XrEyeTrackerCreateInfoFB { XrStructureType type; const void* next; };
struct XrEyeGazesInfoFB { XrStructureType type; const void* next; XrSpace baseSpace; XrTime time; };
struct XrEyeGazeFB { XrBool32 isValid; XrPosef gazePose; float gazeConfidence; };
struct XrEyeGazesFB { XrStructureType type; void* next; XrEyeGazeFB gaze[2]; XrTime time; };
struct XrPassthroughCreateInfoFB { XrStructureType type; const void* next; uint64_t flags; };
struct XrPassthroughLayerCreateInfoFB {
    XrStructureType type; const void* next; XrPassthroughFB passthrough; uint64_t flags; int32_t purpose;
};
struct XrCompositionLayerPassthroughFB {
    XrStructureType type; const void* next; uint64_t flags; XrSpace space; XrPassthroughLayerFB layerHandle;
};
struct XrActionSetCreateInfo { XrStructureType type; const void* next; char actionSetName[64]; char localizedActionSetName[128]; uint32_t priority; };
struct XrActionCreateInfo { XrStructureType type; const void* next; char actionName[64]; int32_t actionType; uint32_t countSubactionPaths; const XrPath* subactionPaths; char localizedActionName[128]; };
struct XrActionSuggestedBinding { XrAction action; XrPath binding; };
struct XrInteractionProfileSuggestedBinding { XrStructureType type; const void* next; XrPath interactionProfile; uint32_t countSuggestedBindings; const XrActionSuggestedBinding* suggestedBindings; };
struct XrSessionActionSetsAttachInfo { XrStructureType type; const void* next; uint32_t countActionSets; const XrActionSet* actionSets; };
struct XrActiveActionSet { XrActionSet actionSet; XrPath subactionPath; };
struct XrActionsSyncInfo { XrStructureType type; const void* next; uint32_t countActiveActionSets; const XrActiveActionSet* activeActionSets; };
struct XrActionStateGetInfo { XrStructureType type; const void* next; XrAction action; XrPath subactionPath; };
struct XrActionStateBoolean { XrStructureType type; void* next; XrBool32 currentState; XrBool32 changedSinceLastSync; XrTime lastChangeTime; XrBool32 isActive; };

static const char* kNames[XR_FACE_EXPRESSION2_COUNT_FB] = {
    "BROW_LOWERER_L","BROW_LOWERER_R","CHEEK_PUFF_L","CHEEK_PUFF_R","CHEEK_RAISER_L","CHEEK_RAISER_R",
    "CHEEK_SUCK_L","CHEEK_SUCK_R","CHIN_RAISER_B","CHIN_RAISER_T","DIMPLER_L","DIMPLER_R",
    "EYES_CLOSED_L","EYES_CLOSED_R","EYES_LOOK_DOWN_L","EYES_LOOK_DOWN_R","EYES_LOOK_LEFT_L","EYES_LOOK_LEFT_R",
    "EYES_LOOK_RIGHT_L","EYES_LOOK_RIGHT_R","EYES_LOOK_UP_L","EYES_LOOK_UP_R","INNER_BROW_RAISER_L","INNER_BROW_RAISER_R",
    "JAW_DROP","JAW_SIDEWAYS_LEFT","JAW_SIDEWAYS_RIGHT","JAW_THRUST","LID_TIGHTENER_L","LID_TIGHTENER_R",
    "LIP_CORNER_DEPRESSOR_L","LIP_CORNER_DEPRESSOR_R","LIP_CORNER_PULLER_L","LIP_CORNER_PULLER_R",
    "LIP_FUNNELER_LB","LIP_FUNNELER_LT","LIP_FUNNELER_RB","LIP_FUNNELER_RT","LIP_PRESSOR_L","LIP_PRESSOR_R",
    "LIP_PUCKER_L","LIP_PUCKER_R","LIP_STRETCHER_L","LIP_STRETCHER_R","LIP_SUCK_LB","LIP_SUCK_LT",
    "LIP_SUCK_RB","LIP_SUCK_RT","LIP_TIGHTENER_L","LIP_TIGHTENER_R","LIPS_TOWARD",
    "LOWER_LIP_DEPRESSOR_L","LOWER_LIP_DEPRESSOR_R","MOUTH_LEFT","MOUTH_RIGHT","NOSE_WRINKLER_L","NOSE_WRINKLER_R",
    "OUTER_BROW_RAISER_L","OUTER_BROW_RAISER_R","UPPER_LID_RAISER_L","UPPER_LID_RAISER_R",
    "UPPER_LIP_RAISER_L","UPPER_LIP_RAISER_R","TONGUE_TIP_INTERDENTAL","TONGUE_TIP_ALVEOLAR",
    "TONGUE_FRONT_DORSAL_PALATE","TONGUE_MID_DORSAL_PALATE","TONGUE_BACK_DORSAL_VELAR","TONGUE_OUT","TONGUE_RETREAT"
};

template <typename T>
struct Fn { T call = nullptr; };

struct App {
    JavaVM* vm = nullptr;
    jobject activity = nullptr;
    jmethodID clock = nullptr;
    jmethodID post = nullptr;
    jmethodID toggle = nullptr;
    jmethodID note = nullptr;
    XrInstance instance = nullptr;
    XrSession session = nullptr;
    XrSpace space = nullptr;
    XrFaceTracker2FB face = nullptr;
    XrEyeTrackerFB eyes = nullptr;
    XrPassthroughFB passthrough = nullptr;
    XrPassthroughLayerFB layer = nullptr;
    XrActionSet actions = nullptr;
    XrAction record = nullptr;
    bool wasDown = false;
    bool begun = false;
    Fn<XrResult (*)(const XrLoaderInitInfoAndroidKHR*)> xrInitializeLoaderKHR;
    Fn<XrResult (*)(XrInstance, const char*, PFN_xrVoidFunction*)> xrGetInstanceProcAddr;
    Fn<XrResult (*)(const XrInstanceCreateInfo*, XrInstance*)> xrCreateInstance;
    Fn<XrResult (*)(XrInstance)> xrDestroyInstance;
    Fn<XrResult (*)(XrInstance, const XrSystemGetInfo*, uint64_t*)> xrGetSystem;
    Fn<XrResult (*)(XrInstance, const XrSessionCreateInfo*, XrSession*)> xrCreateSession;
    Fn<XrResult (*)(XrSession)> xrDestroySession;
    Fn<XrResult (*)(XrSession, const XrSessionBeginInfo*)> xrBeginSession;
    Fn<XrResult (*)(XrSession)> xrEndSession;
    Fn<XrResult (*)(XrSession)> xrRequestExitSession;
    Fn<XrResult (*)(XrInstance, XrEventDataBuffer*)> xrPollEvent;
    Fn<XrResult (*)(XrSession, const XrReferenceSpaceCreateInfo*, XrSpace*)> xrCreateReferenceSpace;
    Fn<XrResult (*)(XrSession, const XrFrameWaitInfo*, XrFrameState*)> xrWaitFrame;
    Fn<XrResult (*)(XrSession, const XrFrameBeginInfo*)> xrBeginFrame;
    Fn<XrResult (*)(XrSession, const XrFrameEndInfo*)> xrEndFrame;
    Fn<XrResult (*)(XrInstance, const char*, XrPath*)> xrStringToPath;
    Fn<XrResult (*)(XrSession, const XrFaceTrackerCreateInfo2FB*, XrFaceTracker2FB*)> xrCreateFaceTracker2FB;
    Fn<XrResult (*)(XrFaceTracker2FB, const XrFaceExpressionInfo2FB*, XrFaceExpressionWeights2FB*)> xrGetFaceExpressionWeights2FB;
    Fn<XrResult (*)(XrSession, const XrEyeTrackerCreateInfoFB*, XrEyeTrackerFB*)> xrCreateEyeTrackerFB;
    Fn<XrResult (*)(XrEyeTrackerFB, const XrEyeGazesInfoFB*, XrEyeGazesFB*)> xrGetEyeGazesFB;
    Fn<XrResult (*)(XrSession, const XrPassthroughCreateInfoFB*, XrPassthroughFB*)> xrCreatePassthroughFB;
    Fn<XrResult (*)(XrPassthroughFB)> xrPassthroughStartFB;
    Fn<XrResult (*)(XrSession, const XrPassthroughLayerCreateInfoFB*, XrPassthroughLayerFB*)> xrCreatePassthroughLayerFB;
    Fn<XrResult (*)(XrPassthroughLayerFB)> xrPassthroughLayerResumeFB;
    Fn<XrResult (*)(XrInstance, const XrActionSetCreateInfo*, XrActionSet*)> xrCreateActionSet;
    Fn<XrResult (*)(XrActionSet, const XrActionCreateInfo*, XrAction*)> xrCreateAction;
    Fn<XrResult (*)(XrInstance, const XrInteractionProfileSuggestedBinding*)> xrSuggestInteractionProfileBindings;
    Fn<XrResult (*)(XrSession, const XrSessionActionSetsAttachInfo*)> xrAttachSessionActionSets;
    Fn<XrResult (*)(XrSession, const XrActionsSyncInfo*)> xrSyncActions;
    Fn<XrResult (*)(XrSession, const XrActionStateGetInfo*, XrActionStateBoolean*)> xrGetActionStateBoolean;
};

static App g;

template <typename T>
bool load(T& fn, const char* name) {
    return g.xrGetInstanceProcAddr.call &&
        g.xrGetInstanceProcAddr.call(g.instance, name, reinterpret_cast<PFN_xrVoidFunction*>(&fn.call)) == XR_SUCCESS &&
        fn.call;
}

static void say(const char* text) {
    if (!g.vm || !g.activity || !g.note) return;
    JNIEnv* env = nullptr;
    bool attached = g.vm->GetEnv(reinterpret_cast<void**>(&env), JNI_VERSION_1_6) == JNI_EDETACHED;
    if (attached) g.vm->AttachCurrentThread(&env, nullptr);
    if (!env) return;
    jstring value = env->NewStringUTF(text);
    env->CallVoidMethod(g.activity, g.note, value);
    env->DeleteLocalRef(value);
    if (attached) g.vm->DetachCurrentThread();
}

static double clock_seconds() {
    JNIEnv* env = nullptr;
    bool attached = g.vm->GetEnv(reinterpret_cast<void**>(&env), JNI_VERSION_1_6) == JNI_EDETACHED;
    if (attached) g.vm->AttachCurrentThread(&env, nullptr);
    double value = env ? env->CallDoubleMethod(g.activity, g.clock) : -1.0;
    if (attached) g.vm->DetachCurrentThread();
    return value;
}

static void post_json(const std::string& body) {
    JNIEnv* env = nullptr;
    bool attached = g.vm->GetEnv(reinterpret_cast<void**>(&env), JNI_VERSION_1_6) == JNI_EDETACHED;
    if (attached) g.vm->AttachCurrentThread(&env, nullptr);
    if (!env) return;
    jstring value = env->NewStringUTF(body.c_str());
    env->CallVoidMethod(g.activity, g.post, value);
    env->DeleteLocalRef(value);
    if (attached) g.vm->DetachCurrentThread();
}

static void toggle_record() {
    JNIEnv* env = nullptr;
    bool attached = g.vm->GetEnv(reinterpret_cast<void**>(&env), JNI_VERSION_1_6) == JNI_EDETACHED;
    if (attached) g.vm->AttachCurrentThread(&env, nullptr);
    if (env) env->CallVoidMethod(g.activity, g.toggle);
    if (attached) g.vm->DetachCurrentThread();
}

static void rotate_forward(const XrQuaternionf& q, float& x, float& y, float& z) {
    float vx = 0, vy = 0, vz = -1;
    float tx = 2 * (q.y * vz - q.z * vy);
    float ty = 2 * (q.z * vx - q.x * vz);
    float tz = 2 * (q.x * vy - q.y * vx);
    x = vx + q.w * tx + (q.y * tz - q.z * ty);
    y = vy + q.w * ty + (q.z * tx - q.x * tz);
    z = vz + q.w * tz + (q.x * ty - q.y * tx);
}

static void publish(XrTime time) {
    float weights[XR_FACE_EXPRESSION2_COUNT_FB] = {};
    float confidence[XR_FACE_CONFIDENCE2_COUNT_FB] = {};
    bool faceOk = false;
    if (g.face && g.xrGetFaceExpressionWeights2FB.call) {
        XrFaceExpressionInfo2FB info{XR_TYPE_FACE_EXPRESSION_INFO2_FB, nullptr, time};
        XrFaceExpressionWeights2FB out{XR_TYPE_FACE_EXPRESSION_WEIGHTS2_FB};
        out.weightCount = XR_FACE_EXPRESSION2_COUNT_FB;
        out.weights = weights;
        out.confidenceCount = XR_FACE_CONFIDENCE2_COUNT_FB;
        out.confidences = confidence;
        faceOk = g.xrGetFaceExpressionWeights2FB.call(g.face, &info, &out) == XR_SUCCESS && out.isValid;
    }
    XrEyeGazesFB gaze{XR_TYPE_EYE_GAZES_FB};
    bool eyesOk = false;
    if (g.eyes && g.space && g.xrGetEyeGazesFB.call) {
        XrEyeGazesInfoFB info{XR_TYPE_EYE_GAZES_INFO_FB, nullptr, g.space, time};
        eyesOk = g.xrGetEyeGazesFB.call(g.eyes, &info, &gaze) == XR_SUCCESS;
    }
    if (!faceOk && !eyesOk) return;
    std::string json = "{\"blendshapes\":{";
    if (faceOk) {
        for (int i = 0; i < XR_FACE_EXPRESSION2_COUNT_FB; ++i) {
            if (i) json += ',';
            char number[32];
            snprintf(number, sizeof(number), "\"%s\":%.4f", kNames[i], weights[i]);
            json += number;
        }
    }
    json += "},\"eyes\":{";
    const char* sides[2] = {"left", "right"};
    for (int i = 0; i < 2; ++i) {
        if (i) json += ',';
        float dx = 0, dy = 0, dz = -1;
        float conf = 0;
        float px = 0, py = 0, pz = 0;
        if (eyesOk && gaze.gaze[i].isValid) {
            rotate_forward(gaze.gaze[i].gazePose.orientation, dx, dy, dz);
            conf = gaze.gaze[i].gazeConfidence;
            px = gaze.gaze[i].gazePose.position.x;
            py = gaze.gaze[i].gazePose.position.y;
            pz = gaze.gaze[i].gazePose.position.z;
        }
        char item[192];
        snprintf(item, sizeof(item),
                 "\"%s\":{\"origin\":[%.4f,%.4f,%.4f],\"direction\":[%.4f,%.4f,%.4f],\"confidence\":%.3f}",
                 sides[i], px, py, pz, dx, dy, dz, conf);
        json += item;
    }
    double t = clock_seconds();
    if (t >= 0) {
        char stamp[64];
        snprintf(stamp, sizeof(stamp), "},\"t\":%.4f}", t);
        json += stamp;
    } else {
        json += "}}";
    }
    post_json(json);
}

static void poll_record() {
    if (!g.record || !g.xrSyncActions.call) return;
    XrActiveActionSet active{g.actions, XR_NULL_PATH};
    XrActionsSyncInfo sync{XR_TYPE_ACTIONS_SYNC_INFO, nullptr, 1, &active};
    if (g.xrSyncActions.call(g.session, &sync) != XR_SUCCESS) return;
    XrActionStateGetInfo get{XR_TYPE_ACTION_STATE_GET_INFO, nullptr, g.record, XR_NULL_PATH};
    XrActionStateBoolean state{XR_TYPE_ACTION_STATE_BOOLEAN};
    if (g.xrGetActionStateBoolean.call(g.session, &get, &state) != XR_SUCCESS) return;
    if (state.currentState && state.changedSinceLastSync && !g.wasDown) toggle_record();
    g.wasDown = state.currentState;
}

static void frame() {
    XrFrameWaitInfo wait{XR_TYPE_FRAME_WAIT_INFO, nullptr};
    XrFrameState state{XR_TYPE_FRAME_STATE};
    if (g.xrWaitFrame.call(g.session, &wait, &state) != XR_SUCCESS) return;
    XrFrameBeginInfo begin{XR_TYPE_FRAME_BEGIN_INFO, nullptr};
    g.xrBeginFrame.call(g.session, &begin);
    publish(state.predictedDisplayTime);
    poll_record();
    XrCompositionLayerPassthroughFB passthrough{XR_TYPE_COMPOSITION_LAYER_PASSTHROUGH_FB, nullptr, 0, g.space, g.layer};
    const XrCompositionLayerBaseHeader* layers[1] = {
        reinterpret_cast<const XrCompositionLayerBaseHeader*>(&passthrough)
    };
    XrFrameEndInfo end{XR_TYPE_FRAME_END_INFO, nullptr, state.predictedDisplayTime,
                       g.layer ? XR_ENVIRONMENT_BLEND_MODE_ALPHA_BLEND : XR_ENVIRONMENT_BLEND_MODE_OPAQUE,
                       g.layer ? 1u : 0u, g.layer ? layers : nullptr};
    g.xrEndFrame.call(g.session, &end);
}

static bool create_record_action() {
    XrActionSetCreateInfo set{XR_TYPE_ACTION_SET_CREATE_INFO};
    snprintf(set.actionSetName, sizeof(set.actionSetName), "rgb");
    snprintf(set.localizedActionSetName, sizeof(set.localizedActionSetName), "RGB");
    if (g.xrCreateActionSet.call(g.instance, &set, &g.actions) != XR_SUCCESS) return false;
    XrActionCreateInfo action{XR_TYPE_ACTION_CREATE_INFO};
    snprintf(action.actionName, sizeof(action.actionName), "record");
    snprintf(action.localizedActionName, sizeof(action.localizedActionName), "Record");
    action.actionType = XR_ACTION_TYPE_BOOLEAN_INPUT;
    if (g.xrCreateAction.call(g.actions, &action, &g.record) != XR_SUCCESS) return false;
    XrPath profile = 0, click = 0;
    g.xrStringToPath.call(g.instance, "/interaction_profiles/oculus/touch_controller", &profile);
    g.xrStringToPath.call(g.instance, "/user/hand/right/input/a/click", &click);
    XrActionSuggestedBinding binding{g.record, click};
    XrInteractionProfileSuggestedBinding suggested{XR_TYPE_INTERACTION_PROFILE_SUGGESTED_BINDING, nullptr, profile, 1, &binding};
    g.xrSuggestInteractionProfileBindings.call(g.instance, &suggested);
    XrSessionActionSetsAttachInfo attach{XR_TYPE_SESSION_ACTION_SETS_ATTACH_INFO, nullptr, 1, &g.actions};
    return g.xrAttachSessionActionSets.call(g.session, &attach) == XR_SUCCESS;
}

static void on_state(int32_t state) {
    if (state == XR_SESSION_STATE_READY && !g.begun) {
        XrSessionBeginInfo info{XR_TYPE_SESSION_BEGIN_INFO, nullptr, XR_VIEW_CONFIGURATION_TYPE_PRIMARY_STEREO};
        if (g.xrBeginSession.call(g.session, &info) == XR_SUCCESS) g.begun = true;
    } else if (state == XR_SESSION_STATE_STOPPING && g.begun) {
        g.xrEndSession.call(g.session);
        g.begun = false;
    } else if (state == XR_SESSION_STATE_EXITING || state == XR_SESSION_STATE_LOSS_PENDING) {
        g.xrRequestExitSession.call(g.session);
    }
}

static bool boot(jobject activity) {
    void* handle = dlopen("libopenxr_loader.so", RTLD_NOW | RTLD_NOLOAD);
    if (!handle) handle = dlopen("libopenxr_loader.so", RTLD_NOW);
    if (!handle) {
        say("OpenXR loader missing. Copy libopenxr_loader.so into jniLibs.");
        return false;
    }
    auto get = reinterpret_cast<XrResult (*)(XrInstance, const char*, PFN_xrVoidFunction*)>(dlsym(handle, "xrGetInstanceProcAddr"));
    if (!get) return false;
    g.xrGetInstanceProcAddr.call = get;
    get(nullptr, "xrInitializeLoaderKHR", reinterpret_cast<PFN_xrVoidFunction*>(&g.xrInitializeLoaderKHR.call));
    get(nullptr, "xrCreateInstance", reinterpret_cast<PFN_xrVoidFunction*>(&g.xrCreateInstance.call));
    if (!g.xrCreateInstance.call) return false;
    JavaVM* vm = g.vm;
    XrLoaderInitInfoAndroidKHR loader{XR_TYPE_LOADER_INIT_INFO_ANDROID_KHR, nullptr, vm, activity};
    if (g.xrInitializeLoaderKHR.call) g.xrInitializeLoaderKHR.call(&loader);
    const char* wanted[] = {
        "XR_KHR_android_create_instance", "XR_FB_face_tracking2", "XR_FB_eye_tracking_social",
        "XR_FB_passthrough", "XR_EXT_eye_gaze_interaction"
    };
    XrInstanceCreateInfoAndroidKHR android{XR_TYPE_INSTANCE_CREATE_INFO_ANDROID_KHR, nullptr, vm, activity};
    XrInstanceCreateInfo info{XR_TYPE_INSTANCE_CREATE_INFO};
    info.next = &android;
    snprintf(info.applicationInfo.applicationName, 128, "RGB Face");
    snprintf(info.applicationInfo.engineName, 128, "rgbmo");
    info.applicationInfo.apiVersion = (1ULL << 48);
    info.enabledExtensionCount = 4;
    info.enabledExtensionNames = wanted;
    if (g.xrCreateInstance.call(&info, &g.instance) != XR_SUCCESS) {
        info.enabledExtensionCount = 3;
        if (g.xrCreateInstance.call(&info, &g.instance) != XR_SUCCESS) {
            say("OpenXR instance was not created. Face tracking permission may be off.");
            return false;
        }
    }
#define BIND(name) load(g.name, #name)
    BIND(xrDestroyInstance); BIND(xrGetSystem); BIND(xrCreateSession); BIND(xrDestroySession);
    BIND(xrBeginSession); BIND(xrEndSession); BIND(xrRequestExitSession); BIND(xrPollEvent);
    BIND(xrCreateReferenceSpace); BIND(xrWaitFrame); BIND(xrBeginFrame); BIND(xrEndFrame); BIND(xrStringToPath);
    BIND(xrCreateFaceTracker2FB); BIND(xrGetFaceExpressionWeights2FB);
    BIND(xrCreateEyeTrackerFB); BIND(xrGetEyeGazesFB);
    BIND(xrCreatePassthroughFB); BIND(xrPassthroughStartFB); BIND(xrCreatePassthroughLayerFB); BIND(xrPassthroughLayerResumeFB);
    BIND(xrCreateActionSet); BIND(xrCreateAction); BIND(xrSuggestInteractionProfileBindings);
    BIND(xrAttachSessionActionSets); BIND(xrSyncActions); BIND(xrGetActionStateBoolean);
#undef BIND
    XrSystemGetInfo systemInfo{XR_TYPE_SYSTEM_GET_INFO, nullptr, XR_FORM_FACTOR_HEAD_MOUNTED_DISPLAY};
    uint64_t system = 0;
    if (g.xrGetSystem.call(g.instance, &systemInfo, &system) != XR_SUCCESS) return false;
    XrSessionCreateInfo session{XR_TYPE_SESSION_CREATE_INFO, nullptr, 0, system};
    if (g.xrCreateSession.call(g.instance, &session, &g.session) != XR_SUCCESS) return false;
    XrPosef identity{};
    identity.orientation.w = 1;
    XrReferenceSpaceCreateInfo space{XR_TYPE_REFERENCE_SPACE_CREATE_INFO, nullptr, XR_REFERENCE_SPACE_TYPE_LOCAL, identity};
    g.xrCreateReferenceSpace.call(g.session, &space, &g.space);
    XrFaceTrackerCreateInfo2FB face{XR_TYPE_FACE_TRACKER_CREATE_INFO2_FB, nullptr, XR_FACE_EXPRESSION_SET2_DEFAULT_FB, 0, nullptr};
    if (!g.xrCreateFaceTracker2FB.call || g.xrCreateFaceTracker2FB.call(g.session, &face, &g.face) != XR_SUCCESS)
        say("Face tracker was not created. Enable face tracking for this app in the headset settings.");
    XrEyeTrackerCreateInfoFB eye{XR_TYPE_EYE_TRACKER_CREATE_INFO_FB, nullptr};
    if (!g.xrCreateEyeTrackerFB.call || g.xrCreateEyeTrackerFB.call(g.session, &eye, &g.eyes) != XR_SUCCESS)
        say("Eye tracker was not created. Enable eye tracking for this app.");
    XrPassthroughCreateInfoFB pass{XR_TYPE_PASSTHROUGH_CREATE_INFO_FB, nullptr, 0};
    if (g.xrCreatePassthroughFB.call && g.xrCreatePassthroughFB.call(g.session, &pass, &g.passthrough) == XR_SUCCESS) {
        g.xrPassthroughStartFB.call(g.passthrough);
        XrPassthroughLayerCreateInfoFB layer{XR_TYPE_PASSTHROUGH_LAYER_CREATE_INFO_FB, nullptr, g.passthrough, 0, 0};
        if (g.xrCreatePassthroughLayerFB.call(g.session, &layer, &g.layer) == XR_SUCCESS)
            g.xrPassthroughLayerResumeFB.call(g.layer);
    }
    create_record_action();
    say(g.face ? "Streaming face and eyes to RGB MoCap" : "Waiting for face tracking permission");
    return true;
}

extern "C" JNIEXPORT void JNICALL
Java_com_rgbmo_facecap_CaptureActivity_nativeRun(JNIEnv* env, jobject thiz, jstring) {
    env->GetJavaVM(&g.vm);
    g.activity = env->NewGlobalRef(thiz);
    jclass cls = env->GetObjectClass(thiz);
    g.clock = env->GetMethodID(cls, "clock", "()D");
    g.post = env->GetMethodID(cls, "postFrame", "(Ljava/lang/String;)V");
    g.toggle = env->GetMethodID(cls, "toggleRecord", "()V");
    g.note = env->GetMethodID(cls, "note", "(Ljava/lang/String;)V");
    if (!boot(g.activity)) return;
    bool exit = false;
    while (!exit) {
        XrEventDataBuffer event{XR_TYPE_EVENT_DATA_BUFFER};
        while (g.xrPollEvent.call(g.instance, &event) == XR_SUCCESS) {
            if (event.type == XR_TYPE_EVENT_DATA_SESSION_STATE_CHANGED) {
                auto* changed = reinterpret_cast<XrEventDataSessionStateChanged*>(&event);
                on_state(changed->state);
                if (changed->state == XR_SESSION_STATE_EXITING || changed->state == XR_SESSION_STATE_LOSS_PENDING)
                    exit = true;
            }
            event.type = XR_TYPE_EVENT_DATA_BUFFER;
        }
        if (g.begun) frame();
    }
}
