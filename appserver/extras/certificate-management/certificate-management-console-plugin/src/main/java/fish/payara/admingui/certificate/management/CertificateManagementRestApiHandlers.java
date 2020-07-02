/*
 * DO NOT ALTER OR REMOVE COPYRIGHT NOTICES OR THIS HEADER.
 *
 * Copyright (c) 2020 Payara Foundation and/or its affiliates. All rights reserved.
 *
 * The contents of this file are subject to the terms of either the GNU
 * General Public License Version 2 only ("GPL") or the Common Development
 * and Distribution License("CDDL") (collectively, the "License").  You
 * may not use this file except in compliance with the License.  You can
 * obtain a copy of the License at
 * https://github.com/payara/Payara/blob/master/LICENSE.txt
 * See the License for the specific
 * language governing permissions and limitations under the License.
 *
 * When distributing the software, include this License Header Notice in each
 * file and include the License file at glassfish/legal/LICENSE.txt.
 *
 * GPL Classpath Exception:
 * The Payara Foundation designates this particular file as subject to the "Classpath"
 * exception as provided by the Payara Foundation in the GPL Version 2 section of the License
 * file that accompanied this code.
 *
 * Modifications:
 * If applicable, add the following below the License Header, with the fields
 * enclosed by brackets [] replaced by your own identifying information:
 * "Portions Copyright [year] [name of copyright owner]"
 *
 * Contributor(s):
 * If you wish your version of this file to be governed by only the CDDL or
 * only the GPL Version 2, indicate your decision by adding "[Contributor]
 * elects to include this software in this distribution under the [CDDL or GPL
 * Version 2] license."  If you don't indicate a single choice of license, a
 * recipient has the option to distribute your version of this file under
 * either the CDDL, the GPL Version 2 or to extend the choice of license to
 * its licensees as provided above.  However, if you add GPL Version 2 code
 * and therefore, elected the GPL Version 2 license, then the option applies
 * only if the new code is made subject to such option by the copyright
 * holder.
 */

package fish.payara.admingui.certificate.management;

import com.sun.enterprise.config.serverbeans.Config;
import com.sun.enterprise.config.serverbeans.Servers;
import com.sun.jsftemplating.annotation.Handler;
import com.sun.jsftemplating.annotation.HandlerInput;
import com.sun.jsftemplating.annotation.HandlerOutput;
import com.sun.jsftemplating.layout.descriptors.handler.HandlerContext;
import org.glassfish.admingui.common.util.GuiUtil;
import org.glassfish.admingui.common.util.RestUtil;
import org.glassfish.grizzly.config.dom.Protocol;
import org.glassfish.hk2.api.ServiceLocator;
import org.glassfish.internal.api.Globals;
import org.glassfish.orb.admin.config.IiopListener;
import org.glassfish.orb.admin.config.IiopService;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

public class CertificateManagementRestApiHandlers {

    @Handler(id = "py.getListCertificatesOutput",
            input = {
                    @HandlerInput(name = "endpoint", type = String.class, required = true),
                    @HandlerInput(name = "contextPath", type = String.class, required = true)
            },
            output = {
                    @HandlerOutput(name = "certificates", type = List.class),
                    @HandlerOutput(name = "usedByLinks", type = Map.class)
            })
    public static void getListCertificatesOutput(HandlerContext handlerCtx) {
        String endpoint = (String) handlerCtx.getInputValue("endpoint");
        String contextPath = (String) handlerCtx.getInputValue("contextPath");

        try {
            // Run the list-certificates to get the instance entries
            Map responseMap = RestUtil.restRequest(endpoint, null, "GET", handlerCtx, false, true);
            Map data = (Map) responseMap.get("data");

            // Get instance name from URL
            int beginIndex = endpoint.indexOf("/server/") + 8;
            String instanceName = endpoint.substring(beginIndex, endpoint.indexOf("/", beginIndex));

            // Get instance config
            ServiceLocator serviceLocator = Globals.getDefaultBaseServiceLocator();
            Servers servers = serviceLocator.getService(Servers.class);
            Config config = servers.getServer(instanceName).getConfig();

            // Get store entries from instance
            Map<String, String> usedByLinks = new HashMap<>();
            List<Map<String, String>> storeEntries = getInstanceEntries(data, contextPath, config, usedByLinks);

            // If we couldn't get the instance store entries, don't try to get the listener store entries
            if (storeEntries.isEmpty()) {
                handlerCtx.setOutputValue("certificates", storeEntries);
                handlerCtx.setOutputValue("usedByLinks", usedByLinks);
                return;
            }

            // Get HTTP and IIOP listener names
            List<String> listeners = new ArrayList<>();
            getAllListenerNamesAndUrls(contextPath, config, serviceLocator, listeners, usedByLinks);

            // Get store entries from HTTP and IIOP listeners
            List<Map<String, String>> listenerStoreEntries = getListenerStoreEntries(endpoint, handlerCtx, listeners);

            // Merge lists
            addListenerStoreEntries(storeEntries, listenerStoreEntries);

            handlerCtx.setOutputValue("certificates", storeEntries);
            handlerCtx.setOutputValue("usedByLinks", usedByLinks);
        } catch (Exception ex) {
            GuiUtil.handleException(handlerCtx, ex);
        }
    }

    private static List<Map<String, String>> getInstanceEntries(Map data, String contextPath,
            Config config, Map<String, String> usedByLinks) {
        List<Map<String, String>> instanceStoreEntries = getEntries(data);

        String instanceConfigUrl = contextPath + "/common/configuration/configuration.jsf?configName="
                + config.getName();

        // Populate "usedBy"
        if (!instanceStoreEntries.isEmpty()) {
            for (Map<String, String> instanceStoreEntry : instanceStoreEntries) {
                instanceStoreEntry.put("usedBy", "Instance JVM");
                usedByLinks.put("Instance JVM", instanceConfigUrl);

            }
        }

        return instanceStoreEntries;
    }

    private static List<Map<String, String>> getEntries(Map data) {
        List<Map<String, String>> storeEntries = new ArrayList<>();
        if (data != null) {
            Map extraProperties = (Map) data.get("extraProperties");
            if (extraProperties != null) {
                storeEntries = (List<Map<String, String>>) extraProperties.get("entries");
                if (storeEntries == null) {
                    // Check if it's in a subreport
                    List<Map> subreports = (List<Map>) data.get("subReports");
                    if (subreports != null) {
                        for (Map subreport : subreports) {
                            extraProperties = (Map) subreport.get("extraProperties");
                            if (extraProperties != null) {
                                storeEntries = (List<Map<String, String>>) extraProperties.get("entries");
                            }
                        }
                    }

                    // Return an empty list if no entries found
                    if (storeEntries == null) {
                        return new ArrayList<>();
                    }
                }
            }
        }
        return storeEntries;
    }

    private static void getAllListenerNamesAndUrls(String contextPath, Config config,
            ServiceLocator serviceLocator, List<String> listeners, Map<String, String> usedByLinks) {

        List<Protocol> protocols = config.getNetworkConfig().getProtocols().getProtocol();

        String httpConfigUrl = contextPath + "/web/grizzly/networkListenerEdit.jsf?configName=" + config.getName()
                + "&cancelTo=web/grizzly/networkListeners.jsf";
        for (Protocol protocol : protocols) {
            listeners.add(protocol.getName());
            usedByLinks.put(protocol.getName(), httpConfigUrl + "&name=" + protocol.getName());
        }

        IiopService iiopService = serviceLocator.getService(IiopService.class);
        String iiopConfigUrl = contextPath + "/corba/sslEdit.jsf?configName=" + config.getName();
        List<IiopListener> iiopListeners = iiopService.getIiopListener();
        for (IiopListener listener : iiopListeners) {
            listeners.add(listener.getId());
            usedByLinks.put(listener.getId(), iiopConfigUrl + "&name=" + listener.getId());
        }
    }

    private static List<Map<String, String>> getListenerStoreEntries(String endpoint, HandlerContext handlerCtx,
            List<String> listenerNames) {
        List<Map<String, String>> allListenerStoreEntries = new ArrayList<>();

        for (String listenerName : listenerNames) {
            Map responseMap;
            if (endpoint.contains("?")) {
                responseMap = RestUtil.restRequest(endpoint + "&listener=" + listenerName, null, "GET",
                        handlerCtx, false, true);
            } else {
                responseMap = RestUtil.restRequest(endpoint + "?listener=" + listenerName, null, "GET",
                        handlerCtx, false, true);
            }

            Map data = (Map) responseMap.get("data");

            List<Map<String, String>> listenerStoreEntries = getEntries(data);

            // Populate "usedBy"
            if (!listenerStoreEntries.isEmpty()) {
                for (Map<String, String> listenerStoreEntry : listenerStoreEntries) {
                    listenerStoreEntry.put("usedBy", listenerName);
                }
            }

            allListenerStoreEntries.addAll(listenerStoreEntries);
        }

        return allListenerStoreEntries;
    }

    private static void addListenerStoreEntries(List<Map<String, String>> storeEntries,
            List<Map<String, String>> listenerStoreEntries) {
        String instanceStore = storeEntries.get(0).get("store");

        for (Map<String, String> listenerStoreEntry : listenerStoreEntries) {
            // Add to list if unique store
            if (!listenerStoreEntry.get("store").equals(instanceStore)) {
                storeEntries.add(listenerStoreEntry);
            } else {
                // if store isn't unique, add name to "usedBy"
                for (Map<String, String> storeEntry : storeEntries) {
                    if (storeEntry.get("alias").equals(listenerStoreEntry.get("alias"))) {
                        storeEntry.put("usedBy", storeEntry.get("usedBy") + "," + listenerStoreEntry.get("usedBy"));
                    }
                }
            }
        }
    }

    @Handler(id = "py.convertUsedByCsvToList",
            input = {
                    @HandlerInput(name = "usedByString", type = String.class, required = true)
            },
            output = {
                    @HandlerOutput(name = "result", type = List.class)
            })
    public static void convertUsedByCsvToList(HandlerContext handlerCtx) {
        String usedByString = (String) handlerCtx.getInputValue("usedByString");

        String[] splitString = usedByString.split(",");
        List<String> usedByList = Arrays.asList(splitString);

        handlerCtx.setOutputValue("result", usedByList);
    }

    @Handler(id = "py.getUsedByLink",
            input = {
                    @HandlerInput(name = "usedBy", type = String.class, required = true),
                    @HandlerInput(name = "usedByLinks", type = Map.class, required = true)
            },
            output = {
                    @HandlerOutput(name = "usedByLink", type = String.class)
            })
    public static void getUsedByLink(HandlerContext handlerCtx) {
        String usedBy = (String) handlerCtx.getInputValue("usedBy");
        Map<String, String> usedByLinks = (Map) handlerCtx.getInputValue("usedByLinks");

        handlerCtx.setOutputValue("usedByLink", usedByLinks.get(usedBy));
    }
}
