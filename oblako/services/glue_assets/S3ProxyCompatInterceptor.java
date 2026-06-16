package io.oblako.glue;

import java.time.Instant;
import java.util.List;

import software.amazon.awssdk.core.SdkResponse;
import software.amazon.awssdk.core.interceptor.Context;
import software.amazon.awssdk.core.interceptor.ExecutionAttributes;
import software.amazon.awssdk.core.interceptor.ExecutionInterceptor;
import software.amazon.awssdk.http.SdkHttpMethod;
import software.amazon.awssdk.http.SdkHttpRequest;
import software.amazon.awssdk.http.SdkHttpResponse;
import software.amazon.awssdk.services.s3.model.ListObjectsResponse;
import software.amazon.awssdk.services.s3.model.ListObjectsV2Response;
import software.amazon.awssdk.services.s3.model.S3Object;

/**
 * Compatibility shims that let Glue's AWS SDK v2 talk to oblako's S3Proxy
 * backend. Registered on every job via
 * {@code spark.hadoop.fs.s3a.audit.execution.interceptors}.
 *
 * <p>Request side — drop the {@code x-amz-optional-object-attributes:
 * RestoreStatus} header the SDK adds to listObjectsV2 (a Glacier optimization);
 * S3Proxy 501s on it ("functionality not implemented").
 *
 * <p>Delete side — S3Proxy 500s on {@code DELETE} of a trailing-slash
 * directory-marker key (e.g. {@code .../_temporary/0/}). S3A deletes those
 * markers while renaming committed output, so the 500 fails the whole commit
 * even though the data is already copied. Such markers are harmless zero-byte
 * objects, so treat the 500 as a successful no-content delete.
 *
 * <p>Response side — S3Proxy omits {@code <Size>} and {@code <LastModified>}
 * for the zero-byte directory markers Spark's FileOutputCommitter writes (keys
 * ending in {@code /}), so those list entries come back with {@code size()} and
 * {@code lastModified()} null. hadoop-aws dereferences both while committing
 * ({@code size().longValue()}, {@code lastModified().toEpochMilli()}) and NPEs —
 * the data is already written; only the commit bookkeeping fails. Default null
 * size to {@code 0L} and null lastModified to the epoch.
 */
public class S3ProxyCompatInterceptor implements ExecutionInterceptor {
    @Override
    public SdkHttpRequest modifyHttpRequest(
            Context.ModifyHttpRequest context, ExecutionAttributes executionAttributes) {
        return context.httpRequest().toBuilder()
                .removeHeader("x-amz-optional-object-attributes")
                .build();
    }

    @Override
    public SdkHttpResponse modifyHttpResponse(
            Context.ModifyHttpResponse context, ExecutionAttributes executionAttributes) {
        SdkHttpResponse response = context.httpResponse();
        SdkHttpRequest request = context.httpRequest();
        if (response.statusCode() == 500
                && request.method() == SdkHttpMethod.DELETE
                && request.encodedPath().endsWith("/")) {
            return response.toBuilder().statusCode(204).statusText("No Content").build();
        }
        return response;
    }

    @Override
    public SdkResponse modifyResponse(
            Context.ModifyResponse context, ExecutionAttributes executionAttributes) {
        SdkResponse response = context.response();
        if (response instanceof ListObjectsV2Response r && r.hasContents()) {
            return r.toBuilder().contents(defaultMarkerFields(r.contents())).build();
        }
        if (response instanceof ListObjectsResponse r && r.hasContents()) {
            return r.toBuilder().contents(defaultMarkerFields(r.contents())).build();
        }
        return response;
    }

    /** Fill the null size / lastModified S3Proxy leaves off directory markers. */
    private static List<S3Object> defaultMarkerFields(List<S3Object> contents) {
        return contents.stream()
                .map(o -> (o.size() != null && o.lastModified() != null)
                        ? o
                        : o.toBuilder()
                                .size(o.size() == null ? 0L : o.size())
                                .lastModified(
                                        o.lastModified() == null
                                                ? Instant.EPOCH
                                                : o.lastModified())
                                .build())
                .toList();
    }
}
