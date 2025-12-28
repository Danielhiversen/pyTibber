"""Gql queries"""

HISTORIC_DATA = """
                {{
                  viewer {{
                    home(id: "{0}") {{
                      {1}(resolution: {2}, last: {3}, before: "{5}") {{
                        pageInfo {{
                          hasPreviousPage
                          startCursor
                        }}
                        nodes {{
                          from
                          unitPrice
                          {4}
                          {1}
                        }}
                      }}
                    }}
                  }}
                }}
          """
HISTORIC_DATA_DATE = """
                    {{
                      viewer {{
                        home(id: "{0}") {{
                          {1}(resolution: {2}, first: {3}, after: "{4}") {{
                            nodes {{
                              from
                              to
                              unitPrice
                              unitPriceVAT
                              currency
                              {5}
                            }}
                          }}
                        }}
                      }}
                    }}
                    """
HISTORIC_PRICE = """
                {{
                  viewer {{
                    home(id: "{0}") {{
                      currentSubscription {{
                        priceRating {{
                            {1} {{
                              entries {{
                                  time
                                  total
                              }}
                            }}
                         }}
                     }}
                  }}
                  }}
                }}
          """
INFO = """
        {
          viewer {
            name
            userId
            homes {
              id
              subscriptions {
                status
              }
            }
            websocketSubscriptionUrl
          }
        }
        """
LIVE_SUBSCRIBE = """
            subscription{
              liveMeasurement(homeId:"%s"){
                accumulatedConsumption
                accumulatedConsumptionLastHour
                accumulatedCost
                accumulatedProduction
                accumulatedProductionLastHour
                accumulatedReward
                averagePower
                currency
                currentL1
                currentL2
                currentL3
                lastMeterConsumption
                lastMeterProduction
                maxPower
                minPower
                power
                powerFactor
                powerProduction
                powerReactive
                signalStrength
                timestamp
                voltagePhase1
                voltagePhase2
                voltagePhase3
            }
           }
        """
PUSH_NOTIFICATION = """
        mutation{{
          sendPushNotification(input: {{
            title: "{}",
            message: "{}",
          }}){{
            successful
            pushedToNumberOfDevices
          }}
        }}
        """
UPDATE_CURRENT_PRICE = """
        {
          viewer {
            home(id: "%s") {
              currentSubscription {
                priceInfo {
                  current {
                    energy
                    tax
                    total
                    startsAt
                  }
                }
              }
            }
          }
        }
        """

UPDATE_INFO_PRICE = """
        {
          viewer {
            home(id: "%s") {
              currentSubscription {
                priceInfo(resolution: %s) {
                  current {
                    currency
                    energy
                    tax
                    total
                    startsAt
                    level
                  }
                  today {
                    total
                    startsAt
                    level
                  }
                  tomorrow {
                    total
                    startsAt
                    level
                  }
                }
              }
              appNickname
              features {
                realTimeConsumptionEnabled
              }
              currentSubscription {
                status
              }
              address {
                address1
                address2
                address3
                city
                postalCode
                country
                latitude
                longitude
              }
              meteringPointData {
                consumptionEan
                energyTaxType
                estimatedAnnualConsumption
                gridCompany
                productionEan
                vatType
              }
              owner {
                name
                isCompany
                language
                contactInfo {
                  email
                  mobile
                }
              }
              timeZone
              subscriptions {
                id
                status
                validFrom
                validTo
                statusReason
              }
            }
          }
        }

        """
